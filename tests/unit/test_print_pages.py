import importlib
import inspect
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from cps import editbooks, print_pages, ub
from cps.services import inkly

kosync = importlib.import_module("cps.progress_syncing.protocols.kosync")
pytestmark = pytest.mark.unit


@pytest.fixture
def session(monkeypatch):
    engine = create_engine("sqlite://")
    # Only the new table is needed; it has no dependency on a reader account.
    print_pages.PrintPageCount.__table__.create(engine)
    with Session(engine) as session:
        monkeypatch.setattr(ub, "session", session)
        yield session
    engine.dispose()


@pytest.mark.parametrize("value", [0, "-1", "0", "380.5", "380.0", "100001", "nan", "abc", "٣٨٠"])
def test_invalid_page_counts_are_rejected(value):
    with pytest.raises(ValueError):
        print_pages.parse_page_count(value)


def test_counts_follow_uuid_and_can_be_changed_and_cleared(session):
    print_pages.set_page_count("book-one", "380")
    session.commit()
    assert print_pages.get_page_count("book-one") == 380
    assert print_pages.get_page_count("book-two") is None
    print_pages.set_page_count("book-one", " 420 ")
    session.commit()
    assert print_pages.get_page_count("book-one") == 420
    print_pages.set_page_count("book-one", "")
    session.commit()
    assert print_pages.get_page_count("book-one") is None


def test_print_count_is_explicit_in_inkly_metadata(session):
    book = SimpleNamespace(id=42, uuid="book-one", title="Example", total_pages=814)
    print_pages.set_page_count(book.uuid, "380")
    session.commit()
    payload = inkly.build_inkly_book(book)
    assert payload["totalPages"] == payload["printPageCount"] == 380
    print_pages.set_page_count(book.uuid, None)
    session.commit()
    payload = inkly.build_inkly_book(book)
    assert payload["totalPages"] == 814
    assert payload["printPageCount"] is None


def test_edit_saves_and_fans_out_to_existing_inkly_connections(session, monkeypatch):
    app = Flask(__name__)
    app.secret_key = "test-only"
    app.register_blueprint(editbooks.editbook)
    monkeypatch.setattr(editbooks, "_", lambda message: message)
    book = SimpleNamespace(id=42, uuid="book-one")
    lookup = Mock(return_value=book)
    queue = Mock()
    monkeypatch.setattr(editbooks.calibre_db, "get_filtered_book", lookup)
    monkeypatch.setattr(inkly, "queue_book_metadata_for_users", queue)
    with app.test_request_context(method="POST", data={"print_page_count": "380"}):
        response = inspect.unwrap(editbooks.edit_print_pages)(42)
        assert response.status_code == 302
    lookup.assert_called_once_with(42, allow_show_archived=True)
    assert print_pages.get_page_count("book-one") == 380
    queue.assert_called_once_with(book)
    with app.test_request_context(method="POST", data={"print_page_count": "3.8"}):
        assert inspect.unwrap(editbooks.edit_print_pages)(42).status_code == 302
    assert print_pages.get_page_count("book-one") == 380
    assert queue.call_count == 1


def test_kosync_metadata_arrives_even_before_the_first_progress_sync(session, monkeypatch):
    print_pages.set_page_count("book-one", 380)
    session.commit()
    monkeypatch.setattr(kosync, "_require_kosync_enabled", lambda: None)
    monkeypatch.setattr(kosync, "authenticate_user", lambda: SimpleNamespace(id=7))
    monkeypatch.setattr(kosync, "get_book_by_checksum", lambda _: (42, "EPUB", "Example", "path", "1"))
    monkeypatch.setattr(kosync.calibre_db if hasattr(kosync, "calibre_db") else editbooks.calibre_db,
                        "get_book", lambda _: SimpleNamespace(uuid="book-one"))
    monkeypatch.setattr(kosync, "get_progress_record", lambda *args: None)
    with Flask(__name__).test_request_context():
        response, status = kosync.get_progress("checksum")
    assert status == 200
    assert response.get_json()["print_page_count"] == 380
    assert "percentage" not in response.get_json()
    print_pages.set_page_count("book-one", None)
    session.commit()
    with Flask(__name__).test_request_context():
        assert kosync.get_progress("checksum")[0].get_json()["print_page_count"] is None


def test_kosync_metadata_requires_authentication(monkeypatch):
    monkeypatch.setattr(kosync, "_require_kosync_enabled", lambda: None)
    monkeypatch.setattr(kosync, "authenticate_user", lambda: None)
    lookup = Mock()
    monkeypatch.setattr(kosync, "get_book_by_checksum", lookup)
    with Flask(__name__).test_request_context():
        response, status = kosync.get_progress("checksum")
    assert status == 400
    assert response.get_json()["error"] == kosync.ERROR_UNAUTHORIZED_USER
    lookup.assert_not_called()
