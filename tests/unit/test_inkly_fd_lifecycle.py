import errno
import gc
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest
import requests

from cps import inkly_outbox
from cps.services import inkly

ATTEMPTS = 1000


class TrackingResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.close_count = 0

    def close(self):
        self.close_count += 1


class FakeQuery:
    def __init__(self, values):
        self.values = values

    def filter(self, *_args):
        return self

    def order_by(self, *_args):
        return self

    def limit(self, _limit):
        return self

    def all(self):
        return self.values

    def first(self):
        return self.values[0] if self.values else None


class FakeSession:
    def __init__(self, events, user):
        self.events = events
        self.user = user
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0

    def query(self, model):
        if model is inkly_outbox.InklyOutboxEvent:
            return FakeQuery(self.events)
        return FakeQuery([self.user])

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1

    def close(self):
        self.close_count += 1


def _events(count):
    return [
        SimpleNamespace(
            id=index,
            user_id=7,
            event_id=f"event-{index}",
            event_type="reading.state",
            payload={"eventId": f"event-{index}", "book": {"uuid": "book-uuid"}},
            created_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
            attempt_count=0,
            last_attempt_at=None,
            next_attempt_at=None,
            last_error=None,
            status=inkly_outbox.OUTBOX_STATUS_PENDING,
            terminal_error=False,
            delivered_at=None,
        )
        for index in range(count)
    ]


def _user(base_url="https://inkly.example"):
    return SimpleNamespace(
        id=7,
        inkly_enabled=True,
        inkly_base_url=base_url,
        inkly_token="test-token",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status_code", "headers", "result_key"),
    [
        (200, {}, "delivered"),
        (401, {}, "auth_failed"),
        (429, {"Retry-After": "1"}, "retried"),
        (500, {}, "retried"),
        (503, {"Retry-After": "2"}, "retried"),
    ],
)
def test_high_volume_http_responses_are_closed_once(
    monkeypatch, status_code, headers, result_key
):
    responses = []

    def send(_config, _payload):
        response = TrackingResponse(status_code, headers)
        responses.append(response)
        return response

    monkeypatch.setattr(inkly, "send_inkly_event", send)
    monkeypatch.setattr(inkly_outbox, "_log_delivery_failure", lambda *_args: None)
    session = FakeSession(_events(ATTEMPTS), _user())

    result = inkly_outbox.deliver_due_inkly_events(
        limit=ATTEMPTS,
        now=datetime(2026, 8, 30, tzinfo=timezone.utc),
        _session=session,
    )

    assert result[result_key] == ATTEMPTS
    assert len(responses) == ATTEMPTS
    assert all(response.close_count == 1 for response in responses)
    assert session.rollback_count == 0
    assert session.close_count == 0  # The caller owns injected sessions.


@pytest.mark.unit
@pytest.mark.parametrize(
    ("exception_type", "expected_error"),
    [
        (requests.exceptions.Timeout, "network_timeout"),
        (requests.exceptions.ConnectionError, "network_error"),
    ],
)
def test_high_volume_exception_responses_are_closed_once(
    monkeypatch, exception_type, expected_error
):
    responses = []

    def send(_config, _payload):
        response = TrackingResponse(599)
        responses.append(response)
        raise exception_type("simulated transport failure", response=response)

    monkeypatch.setattr(inkly, "send_inkly_event", send)
    monkeypatch.setattr(inkly_outbox, "_log_delivery_failure", lambda *_args: None)
    events = _events(ATTEMPTS)
    session = FakeSession(events, _user())

    result = inkly_outbox.deliver_due_inkly_events(
        limit=ATTEMPTS,
        now=datetime(2026, 8, 30, tzinfo=timezone.utc),
        _session=session,
    )

    assert result["retried"] == ATTEMPTS
    assert all(event.last_error == expected_error for event in events)
    assert len(responses) == ATTEMPTS
    assert all(response.close_count == 1 for response in responses)
    assert session.rollback_count == 0


@pytest.mark.unit
def test_worker_closes_only_sessions_it_creates(monkeypatch):
    session = FakeSession([], _user())
    monkeypatch.setattr(inkly_outbox, "_new_session", lambda: session)

    inkly_outbox.deliver_due_inkly_events()

    assert session.close_count == 1


@pytest.mark.unit
def test_session_creation_emfile_reports_fd_diagnostics(monkeypatch):
    messages = []
    monkeypatch.setattr(
        inkly_outbox,
        "_new_session",
        lambda: (_ for _ in ()).throw(OSError(errno.EMFILE, "too many open files")),
    )
    monkeypatch.setattr(
        inkly_outbox.log,
        "error",
        lambda message, *args: messages.append(message % args if args else message),
    )

    result = inkly_outbox.deliver_due_inkly_events()

    assert result == {
        "delivered": 0,
        "retried": 0,
        "terminal": 0,
        "auth_failed": 0,
        "skipped": 0,
    }
    assert any("open_fds=" in message and "nofile_soft=" in message for message in messages)


class LocalInklyHandler(BaseHTTPRequestHandler):
    request_count = 0

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length:
            self.rfile.read(content_length)
        type(self).request_count += 1
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def log_message(self, _format, *_args):
        pass


@pytest.mark.unit
@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="Linux /proc is required")
def test_real_local_http_cycles_do_not_grow_file_descriptors():
    LocalInklyHandler.request_count = 0
    server = HTTPServer(("127.0.0.1", 0), LocalInklyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    baseline = len(os.listdir("/proc/self/fd"))
    attempts = 300
    session = FakeSession(
        _events(attempts),
        _user(f"http://127.0.0.1:{server.server_port}"),
    )
    try:
        result = inkly_outbox.deliver_due_inkly_events(
            limit=attempts,
            now=datetime(2026, 8, 30, tzinfo=timezone.utc),
            _session=session,
        )
        gc.collect()
        after = len(os.listdir("/proc/self/fd"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result["delivered"] == attempts
    assert LocalInklyHandler.request_count == attempts
    assert after <= baseline + 4
