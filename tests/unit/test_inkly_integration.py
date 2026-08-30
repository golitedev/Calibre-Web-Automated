from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from cps import inkly_outbox
from cps.services import inkly
from cps.readingservices import prepare_annotation_for_inkly


def _book():
    return SimpleNamespace(
        id=42,
        uuid="cwa-arc-uuid",
        title="ARC Test",
        path="Test Author/ARC Test",
        authors=[SimpleNamespace(name="Test Author")],
        data=[SimpleNamespace(name="ARC Test", format="EPUB")],
        identifiers=[
            SimpleNamespace(type="custom-id", val="manuscript-42"),
            SimpleNamespace(type="doi", val="10.1234/example"),
        ],
        comments=[SimpleNamespace(text="Private test description")],
        publishers=[SimpleNamespace(name="Test Publisher")],
        languages=[SimpleNamespace(lang_code="en")],
        pubdate=datetime(2026, 8, 30, tzinfo=timezone.utc),
    )


def _user(token="token-one"):
    return SimpleNamespace(id=7, inkly_enabled=True, inkly_base_url="https://inkly.example", inkly_token=token)


@pytest.mark.unit
def test_unknown_book_payload_preserves_uuid_and_identifiers_without_isbn():
    payload = inkly.build_inkly_book(_book())
    assert payload["uuid"] == "cwa-arc-uuid"
    assert payload["calibreId"] == 42
    assert payload["title"] == "ARC Test"
    assert payload["authors"] == ["Test Author"]
    assert payload["fileName"] == "ARC Test.epub"
    assert payload["isbn"] is None
    assert payload["isbn10"] is None
    assert payload["isbn13"] is None
    assert payload["identifiers"] == {"custom-id": "manuscript-42", "doi": "10.1234/example"}


@pytest.mark.unit
def test_book_builder_caps_identifiers_without_losing_isbn_metadata():
    book = _book()
    book.identifiers = [
        SimpleNamespace(type=f"custom-{index}", val=f"value-{index}")
        for index in range(50)
    ] + [SimpleNamespace(type="isbn13", val="9780306406157")]
    payload = inkly.build_inkly_book(book)
    assert len(payload["identifiers"]) == 50
    assert payload["isbn13"] == "9780306406157"


@pytest.mark.unit
def test_reading_state_event_keeps_raw_kobo_state_and_queues_metadata(monkeypatch):
    captured = {}

    def capture(user, events, **kwargs):
        captured["events"] = events
        captured["metadata"] = kwargs["metadata_event"]
        return True

    monkeypatch.setattr(inkly_outbox, "enqueue_events", capture)
    state = {
        "CurrentBookmark": {
            "ProgressPercent": 42.5,
            "ContentSourceProgressPercent": 42.5,
            "LastModified": "2026-08-30T12:00:00Z",
        },
        "StatusInfo": {"Status": "Reading"},
        "Statistics": {"SpentReadingMinutes": 120, "RemainingTimeMinutes": 180},
    }
    assert inkly.queue_reading_state(_user(), _book(), state)
    event = captured["events"][0]
    assert event["eventType"] == "reading.state"
    assert event["data"]["state"] == state
    assert event["occurredAt"] == "2026-08-30T12:00:00.000Z"
    assert captured["metadata"]["eventType"] == "book.metadata"


@pytest.mark.unit
def test_annotations_keep_notes_structured_and_chunk_at_inkly_limits(monkeypatch):
    captured = {}

    def capture(user, events, **kwargs):
        captured["events"] = events
        return True

    monkeypatch.setattr(inkly_outbox, "enqueue_events", capture)
    annotations = [
        {
            "id": "highlight-1",
            "clientLastModifiedUtc": "2026-08-30T12:00:00Z",
            "highlightColor": "yellow",
            "highlightedText": "Test highlight",
            "noteText": "Test note",
            "type": "highlight",
            "location": {"span": {"chapterFilename": "chapter.xhtml", "chapterProgress": 0.4}},
        },
        {"id": "note-1", "noteText": "Standalone note", "type": "note"},
    ]
    annotations.extend({"id": f"extra-{index}", "highlightedText": "x"} for index in range(100))
    deleted = [f"deleted-{index}" for index in range(101)]
    assert inkly.queue_annotation_sync(_user(), _book(), annotations, deleted)
    assert len(captured["events"]) == 2
    assert all(len(event["data"]["updatedAnnotations"]) <= 100 for event in captured["events"])
    assert all(len(event["data"]["deletedAnnotationIds"]) <= 100 for event in captured["events"])
    first_annotation = captured["events"][0]["data"]["updatedAnnotations"][0]
    assert first_annotation["highlightedText"] == "Test highlight"
    assert first_annotation["noteText"] == "Test note"
    assert any(
        annotation.get("id") == "note-1"
        for event in captured["events"]
        for annotation in event["data"]["updatedAnnotations"]
    )


@pytest.mark.unit
def test_annotation_progress_is_added_without_discarding_location():
    annotation = {
        "id": "annotation-1",
        "highlightedText": "Test highlight",
        "location": {"span": {"chapterFilename": "chapter.xhtml", "chapterProgress": 0.4}},
    }
    prepared = prepare_annotation_for_inkly(annotation, SimpleNamespace(calculate=lambda *_: 42.5))
    assert prepared["overallPercentage"] == 42.5
    assert prepared["location"] == annotation["location"]


@pytest.mark.unit
def test_http_transport_uses_contract_endpoint_and_bearer_token(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return SimpleNamespace(status_code=200, headers={})

    monkeypatch.setattr(inkly.requests, "post", fake_post)
    config = inkly.get_inkly_config(_user("token-two"))
    inkly.send_inkly_event(config, {"eventId": "event-1"})
    assert captured["url"] == "https://inkly.example/api/integrations/cwa/events"
    assert captured["kwargs"]["headers"]["Authorization"] == "Bearer token-two"
    assert captured["kwargs"]["allow_redirects"] is False
    assert captured["kwargs"]["timeout"] == (3, 10)


@pytest.mark.unit
def test_invalid_urls_are_rejected_before_worker_delivery():
    with pytest.raises(ValueError):
        inkly.normalize_inkly_base_url("ftp://inkly.example")
    with pytest.raises(ValueError):
        inkly.normalize_inkly_base_url("https://user:password@inkly.example")
    with pytest.raises(ValueError):
        inkly.normalize_inkly_base_url("https://inkly.example:invalid")


def _event(attempt_count=1):
    return SimpleNamespace(
        event_id="stable-event-id",
        event_type="reading.state",
        payload={"book": {"uuid": "cwa-arc-uuid"}},
        attempt_count=attempt_count,
        status=inkly_outbox.OUTBOX_STATUS_PENDING,
        terminal_error=False,
        delivered_at=None,
        next_attempt_at=None,
        last_error=None,
    )


@pytest.mark.unit
def test_outbox_response_policy_preserves_event_id_and_handles_retries():
    now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    event = _event()
    response = SimpleNamespace(status_code=503, headers={})
    assert inkly_outbox._finish_response(event, response, now) == "retry"
    assert event.event_id == "stable-event-id"
    assert event.next_attempt_at == now + timedelta(seconds=5)

    response = SimpleNamespace(status_code=429, headers={"Retry-After": "120"})
    assert inkly_outbox._finish_response(event, response, now) == "retry"
    assert event.next_attempt_at == now + timedelta(seconds=120)


@pytest.mark.unit
def test_outbox_network_failure_uses_backoff_and_keeps_event_pending():
    now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    event = _event()
    inkly_outbox._finish_exception(event, "network_timeout", now)
    assert event.status == inkly_outbox.OUTBOX_STATUS_PENDING
    assert event.last_error == "network_timeout"
    assert event.next_attempt_at == now + timedelta(seconds=5)


@pytest.mark.unit
def test_metadata_queue_targets_enabled_users_without_network_calls(monkeypatch):
    class FakeColumn:
        def is_(self, value):
            return value

    users = [SimpleNamespace(id=7), SimpleNamespace(id=8)]

    class FakeQuery:
        def filter(self, *_args):
            return self

        def all(self):
            return users

    class FakeSession:
        def query(self, *_args):
            return FakeQuery()

    monkeypatch.setattr(inkly.ub, "User", SimpleNamespace(inkly_enabled=FakeColumn()))
    monkeypatch.setattr(inkly.ub, "session", FakeSession())
    queued = []
    monkeypatch.setattr(
        inkly,
        "_enqueue_with_metadata",
        lambda user, book, events: queued.append(user.id) or True,
    )
    assert inkly.queue_book_metadata_for_users(_book()) == 2
    assert queued == [7, 8]


@pytest.mark.unit
@pytest.mark.parametrize("status", [200, 201, 204])
def test_outbox_2xx_marks_delivered(status):
    event = _event()
    now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    assert inkly_outbox._finish_response(event, SimpleNamespace(status_code=status, headers={}), now) == "delivered"
    assert event.status == inkly_outbox.OUTBOX_STATUS_DELIVERED
    assert event.delivered_at == now
    assert event.event_id == "stable-event-id"


@pytest.mark.unit
@pytest.mark.parametrize("status", [400, 413, 403])
def test_outbox_invalid_payload_responses_are_terminal(status):
    event = _event()
    assert inkly_outbox._finish_response(event, SimpleNamespace(status_code=status, headers={}), datetime.now(timezone.utc)) == "terminal"
    assert event.status == inkly_outbox.OUTBOX_STATUS_TERMINAL
    assert event.terminal_error is True


@pytest.mark.unit
def test_outbox_401_is_inspectable_and_not_immediately_retried():
    event = _event()
    assert inkly_outbox._finish_response(event, SimpleNamespace(status_code=401, headers={}), datetime.now(timezone.utc)) == "auth_failed"
    assert event.status == inkly_outbox.OUTBOX_STATUS_AUTH_FAILED
    assert event.next_attempt_at is None
    assert event.last_error == "authentication_failed"


@pytest.mark.unit
def test_each_user_gets_a_distinct_connection_key():
    first = inkly.get_inkly_config(_user("token-one"))
    second = inkly.get_inkly_config(_user("token-two"))
    assert first["connection_key"] != second["connection_key"]
    assert first["endpoint"] == second["endpoint"]


@pytest.mark.unit
def test_local_reading_services_stub_does_not_mask_enabled_inkly(monkeypatch):
    from cps import app, kobo

    monkeypatch.setattr(kobo, "_local_reading_services_enabled", lambda: True)
    with app.test_request_context("/api/v3/content/cwa-arc-uuid/annotations"):
        assert kobo._kobo_reading_services_stub() is None
