import importlib
import inspect
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from cps import calibre_db, editbooks, print_pages, ub
from cps.services import inkly

kosync = importlib.import_module("cps.progress_syncing.protocols.kosync")
pytestmark = pytest.mark.unit


@pytest.fixture
def session(monkeypatch, tmp_path):
    engine = create_engine("sqlite://")
    app_engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    print_pages.PrintPageCount.__table__.create(app_engine)
    print_pages.PrintPageState.__table__.create(app_engine)
    with engine.begin() as con:
        con.execute(text("CREATE TABLE books(id integer primary key,uuid text unique)"))
        con.execute(text("INSERT INTO books VALUES (42,'book-one'), (43,'book-two')"))
        con.execute(text("CREATE TABLE custom_columns(id integer,label text,datatype text,is_multiple integer,mark_for_delete integer)"))
        con.execute(text("INSERT INTO custom_columns VALUES (7,'real_pages','int',0,0)"))
        con.execute(text("CREATE TABLE custom_column_7(id integer primary key,book integer unique,value integer)"))
    with Session(engine) as session, Session(app_engine) as app_session:
        monkeypatch.setattr(ub, "session", app_session)
        monkeypatch.setattr(ub, "get_new_session_instance", lambda: Session(app_engine))
        monkeypatch.setattr(calibre_db, "session", session)
        yield session
    engine.dispose()
    app_engine.dispose()


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
    monkeypatch.setattr(editbooks.calibre_db, "set_metadata_dirty", Mock())
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
    monkeypatch.setattr(calibre_db, "get_book", lambda _: SimpleNamespace(uuid="book-one"))
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
    session.execute(text("DELETE FROM custom_columns WHERE label='real_pages'"))
    session.commit()
    with Flask(__name__).test_request_context():
        # A temporarily unavailable column is different from an intentional clear.
        assert "print_page_count_managed" not in kosync.get_progress("checksum")[0].get_json()


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


def test_external_calibre_edit_is_the_source_and_advances_sync_revision(session):
    print_pages.set_page_count("book-one", 380)
    session.commit()
    first = print_pages.get_page_count_updated_at("book-one", 380)
    assert print_pages.get_page_count_updated_at("book-one", 380) == first
    # Simulate Calibre editing the ordinary custom column without CWA's route.
    session.execute(text("UPDATE custom_column_7 SET value=420 WHERE book=42"))
    session.commit()
    assert print_pages.get_page_count("book-one") == 420
    assert print_pages.get_page_count_updated_at("book-one", 420) > first
    # Legacy app.db data is no longer authoritative.
    ub.session.add(print_pages.PrintPageCount(book_uuid="book-one", pages=999))
    ub.session.commit()
    assert print_pages.get_page_count("book-one") == 420


def test_revision_observation_does_not_commit_unrelated_app_work(session):
    pending = print_pages.PrintPageCount(book_uuid="pending", pages=10)
    ub.session.add(pending)
    print_pages.get_page_count_updated_at("book-one", 380)
    assert pending in ub.session.new
    ub.session.rollback()


def test_invalid_external_value_or_column_does_not_break_progress_sync(session):
    session.execute(text("INSERT INTO custom_column_7(book,value) VALUES(42,-20)"))
    session.commit()
    assert print_pages.get_page_count("book-one") is None
    session.execute(text("UPDATE custom_columns SET datatype='text' WHERE id=7"))
    session.commit()
    assert print_pages.get_page_count("book-one") is None
    with pytest.raises(ValueError, match="integer"):
        print_pages.set_page_count("book-one", 380)


def test_missing_book_is_rejected_without_creating_a_value(session):
    with pytest.raises(ValueError, match="no longer present"):
        print_pages.set_page_count("deleted-book", 380)
    assert session.execute(text("SELECT count(*) FROM custom_column_7")).scalar() == 0


def test_concurrent_first_syncs_share_one_stable_revision(session):
    with ThreadPoolExecutor(max_workers=4) as workers:
        stamps = list(workers.map(lambda _: print_pages.get_page_count_updated_at("book-one", 380), range(12)))
    assert len(set(stamps)) == 1
