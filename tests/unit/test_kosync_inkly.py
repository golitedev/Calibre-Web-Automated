"""Exercise KOSync requests through Inkly event construction, without delivery."""

import importlib
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask
from sqlalchemy.exc import SQLAlchemyError

from cps import calibre_db, inkly_outbox

kosync = importlib.import_module("cps.progress_syncing.protocols.kosync")


@pytest.fixture
def sync_context(monkeypatch):
    user = SimpleNamespace(
        id=7, inkly_enabled=True, inkly_base_url="https://inkly.example",
        inkly_token="test-only-token",
    )
    book = SimpleNamespace(
        id=42, uuid="calibre-book-uuid", title="KOReader book",
        authors=[SimpleNamespace(name="Test Author")],
    )
    record = SimpleNamespace()
    operations = []
    session = SimpleNamespace(
        commit=Mock(side_effect=lambda: operations.append("commit")),
        rollback=Mock(), add=Mock(),
    )
    enqueue = Mock(side_effect=lambda *args, **kwargs: operations.append("enqueue") or True)
    hardcover = Mock()
    monkeypatch.setattr(kosync, "_require_kosync_enabled", lambda: None)
    monkeypatch.setattr(kosync, "authenticate_user", lambda: user)
    monkeypatch.setattr(kosync, "enrich_response_with_book_info",
                        lambda response, document: (response, book.id, "EPUB", book.title, None))
    monkeypatch.setattr(kosync, "get_progress_record", lambda *args: record)
    monkeypatch.setattr(kosync, "update_book_read_status", Mock())
    monkeypatch.setattr(kosync.ub, "session", session)
    monkeypatch.setattr(calibre_db, "get_book", lambda book_id: book)
    monkeypatch.setattr(inkly_outbox, "enqueue_events", enqueue)
    monkeypatch.setattr(kosync, "push_reading_state_to_hardcover", hardcover)
    # No Inkly HTTP request belongs on the KOSync request path.
    monkeypatch.setattr(kosync.inkly, "send_inkly_event", Mock(side_effect=AssertionError("unexpected HTTP")))

    def request(percentage):
        with Flask(__name__).test_request_context(json={
            "document": "book-checksum", "progress": "/body/section[3]",
            "percentage": percentage, "device": "KOReader", "device_id": "test-device",
        }):
            response, status = kosync.update_progress()
            return response.get_json(), status

    return SimpleNamespace(
        request=request, user=user, book=book, record=record, session=session,
        enqueue=enqueue, hardcover=hardcover, operations=operations,
    )


@pytest.mark.unit
@pytest.mark.parametrize("percentage,expected,status", [
    (0, 0, "ReadyToRead"),
    (0.005, 0.5, "Reading"),
    (0.425, 42.5, "Reading"),
    (0.989, 98.9, "Reading"),
    (0.99, 99, "Finished"),
    (1, 100, "Finished"),
    (42.5, 42.5, "Reading"),
])
def test_kosync_queues_inkly_after_persisting_progress(sync_context, percentage, expected, status):
    ctx = sync_context
    # A failure at the other sink must not prevent Inkly from being queued.
    ctx.hardcover.side_effect = RuntimeError("Hardcover unavailable")
    _, http_status = ctx.request(percentage)
    assert http_status == 200
    assert ctx.operations == ["commit", "commit", "enqueue"]
    args, kwargs = ctx.enqueue.call_args
    assert args[0] is ctx.user
    event, = args[1]
    assert event["eventType"] == "reading.state"
    assert event["book"]["uuid"] == ctx.book.uuid
    assert event["book"]["calibreId"] == ctx.book.id
    state = event["data"]["state"]
    assert state["CurrentBookmark"]["ProgressPercent"] == pytest.approx(expected)
    assert state["StatusInfo"]["Status"] == status
    assert state["KOReaderProgress"] == "/body/section[3]"
    occurred_at = datetime.fromisoformat(event["occurredAt"].replace("Z", "+00:00"))
    assert abs((occurred_at - ctx.record.timestamp).total_seconds()) < 0.001
    assert kwargs["metadata_event"]["eventType"] == "book.metadata"
    assert kwargs["book_uuid"] == ctx.book.uuid
    assert ctx.record.percentage == pytest.approx(expected)


@pytest.mark.unit
def test_inkly_queue_failure_does_not_fail_kosync_or_skip_hardcover(sync_context):
    ctx = sync_context
    ctx.enqueue.side_effect = RuntimeError("outbox unavailable")
    _, status = ctx.request(0.425)
    assert status == 200
    assert ctx.record.percentage == 42.5
    ctx.hardcover.assert_called_once_with(ctx.user, ctx.book, 42)


@pytest.mark.unit
def test_disabled_inkly_does_not_queue_events(sync_context):
    ctx = sync_context
    ctx.user.inkly_enabled = False
    assert ctx.request(0.425)[1] == 200
    ctx.enqueue.assert_not_called()
    ctx.hardcover.assert_called_once()


@pytest.mark.unit
def test_unmatched_document_still_saves_kosync_without_inkly(sync_context, monkeypatch):
    ctx = sync_context
    monkeypatch.setattr(kosync, "enrich_response_with_book_info",
                        lambda response, document: (response, None, None, None, None))
    assert ctx.request(0.425)[1] == 200
    assert ctx.operations == ["commit"]
    assert ctx.record.percentage == 42.5
    ctx.enqueue.assert_not_called()


@pytest.mark.unit
def test_failed_kosync_commit_does_not_queue_inkly(sync_context):
    ctx = sync_context
    ctx.session.commit.side_effect = SQLAlchemyError("commit failed")
    response, status = ctx.request(0.425)
    assert status == 400
    assert response["error"] == kosync.ERROR_INTERNAL
    ctx.enqueue.assert_not_called()
    ctx.hardcover.assert_not_called()


@pytest.mark.unit
def test_first_sync_queues_inkly_for_new_progress_record(sync_context, monkeypatch):
    ctx = sync_context
    monkeypatch.setattr(kosync, "get_progress_record", lambda *args: None)
    assert ctx.request(0.425)[1] == 200
    saved_record = ctx.session.add.call_args.args[0]
    assert saved_record.user_id == ctx.user.id
    assert saved_record.percentage == 42.5
    assert ctx.operations == ["commit", "commit", "enqueue"]
    ctx.enqueue.assert_called_once()
