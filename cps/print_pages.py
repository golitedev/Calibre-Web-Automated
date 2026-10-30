"""Calibre's #real_pages is authoritative; app.db only tracks sync revisions."""

import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, DateTime, Integer, String, text

from . import logger, ub

log = logger.create()
LABEL = "real_pages"
NAME = "Páginas reales"


class PrintPageCount(ub.Base):
    """Legacy rows retained for migration/rollback, never read as live counts."""
    __tablename__ = "cwa_print_page_count"
    book_uuid = Column(String(200), primary_key=True)
    pages = Column(Integer, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (CheckConstraint("pages >= 1 AND pages <= 100000"),)


class PrintPageState(ub.Base):
    __tablename__ = "cwa_print_page_state"
    book_uuid = Column(String(200), primary_key=True)
    pages = Column(Integer, nullable=True)
    updated_at = Column(DateTime, nullable=False)


def parse_page_count(value):
    value = "" if value is None else str(value).strip()
    if not value:
        return None
    if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 100000:
        raise ValueError("Print page count must be a whole number from 1 to 100000.")
    return int(value)


def _library_session(session):
    if session is not None:
        return session
    from . import calibre_db
    return calibre_db.session


def _column_id(session):
    row = session.execute(text(
        "SELECT id, datatype, is_multiple, mark_for_delete FROM custom_columns WHERE label = :label"
    ), {"label": LABEL}).first()
    if not row:
        return None
    if row.datatype != "int" or row.is_multiple or row.mark_for_delete:
        raise ValueError("Calibre #real_pages must be an active, single-value integer column.")
    return int(row.id)


def get_page_count(book_uuid, session=None):
    session = _library_session(session)
    if session is None or not book_uuid:
        return None
    try:
        column = _column_id(session)
    except ValueError as error:
        log.warning("Ignoring unavailable Calibre print page counts: %s", error)
        return None
    if column is None:
        return None
    value = session.execute(text(
        f"SELECT c.value FROM custom_column_{column} c JOIN books b ON b.id = c.book WHERE b.uuid = :uuid"
    ), {"uuid": book_uuid}).scalar()
    try:
        return parse_page_count(value)
    except ValueError:
        # External Calibre edits must not break progress sync for the book.
        log.warning("Ignoring invalid Calibre #real_pages value for book %s", book_uuid)
        return None


def has_page_count_column():
    session = _library_session(None)
    if session is None:
        return False
    try:
        return _column_id(session) is not None
    except ValueError:
        return False


def set_page_count(book_uuid, pages, session=None):
    session = _library_session(session)
    pages = parse_page_count(pages)
    column = _column_id(session)
    if column is None:
        raise ValueError("Create an integer #real_pages column in Calibre, then reconnect the library.")
    book_id = session.execute(text("SELECT id FROM books WHERE uuid = :uuid"), {"uuid": book_uuid}).scalar()
    if book_id is None:
        raise ValueError("Book is no longer present in the Calibre library.")
    table = f"custom_column_{column}"
    params = {"book": book_id, "pages": pages}
    if pages is None:
        session.execute(text(f"DELETE FROM {table} WHERE book = :book"), params)
    elif session.execute(text(f"SELECT id FROM {table} WHERE book = :book"), params).first():
        session.execute(text(f"UPDATE {table} SET value = :pages WHERE book = :book"), params)
    else:
        session.execute(text(f"INSERT INTO {table}(book,value) VALUES (:book,:pages)"), params)


def get_page_count_updated_at(book_uuid, pages):
    """Observe external Calibre changes without committing a caller's work."""
    if not book_uuid or ub.session is None:
        return None
    session = ub.get_new_session_instance()
    try:
        # One atomic upsert avoids racing two first-time sync requests. Repeated
        # observations of the same value keep the original revision timestamp.
        session.execute(text(
            "INSERT INTO cwa_print_page_state(book_uuid,pages,updated_at) VALUES (:uuid,:pages,:stamp) "
            "ON CONFLICT(book_uuid) DO UPDATE SET pages=excluded.pages,updated_at=excluded.updated_at "
            "WHERE cwa_print_page_state.pages IS NOT excluded.pages"
        ), {"uuid": book_uuid, "pages": pages,
            "stamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")})
        row = session.query(PrintPageState).filter_by(book_uuid=book_uuid).first()
        stamp = row.updated_at.replace(tzinfo=timezone.utc).isoformat()
        session.commit()
        return stamp
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _run_calibredb(library_path, *args):
    from . import config
    configured = getattr(config, "config_binariesdir", None)
    binary = os.path.join(configured, "calibredb") if configured else shutil.which("calibredb")
    if not binary:
        raise ValueError("calibredb is needed to create the #real_pages column.")
    subprocess.run([binary, *args, "--with-library", library_path],
                   check=True, capture_output=True, timeout=60)


def prepare_library(library_path, app_db_path):
    """Use Calibre to create its own schema before CWA reflects custom columns.

    Import legacy app.db values once. Existing Calibre values win, and legacy
    rows remain available for rollback. Revision rows mark completed imports,
    so clearing a Calibre value never resurrects an old app.db count.
    """
    path = os.path.join(library_path, "metadata.db")
    with sqlite3.connect(path, timeout=30) as library:
        row = library.execute(
            "SELECT id,datatype,is_multiple,mark_for_delete FROM custom_columns WHERE label=?", (LABEL,)
        ).fetchone()
    if row is None:
        _run_calibredb(library_path, "add_custom_column", LABEL, NAME, "int")
        with sqlite3.connect(path, timeout=30) as library:
            row = library.execute(
                "SELECT id,datatype,is_multiple,mark_for_delete FROM custom_columns WHERE label=?", (LABEL,)
            ).fetchone()
    if row is None or row[1] != "int" or row[2] or row[3]:
        raise ValueError("Calibre #real_pages must be an active, single-value integer column.")
    table = f"custom_column_{int(row[0])}"
    if not app_db_path or not os.path.isfile(app_db_path):
        return
    with sqlite3.connect(app_db_path, timeout=30) as app:
        # Some background tooling initializes the library before the app schema.
        if not all(app.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone()
                   for name in ("cwa_print_page_count", "cwa_print_page_state")):
            return
        legacy = app.execute(
            "SELECT book_uuid,pages FROM cwa_print_page_count WHERE book_uuid NOT IN (SELECT book_uuid FROM cwa_print_page_state)"
        ).fetchall()
        for book_uuid, pages in legacy:
            with sqlite3.connect(path, timeout=30) as library:
                book = library.execute("SELECT id FROM books WHERE uuid=?", (book_uuid,)).fetchone()
                if not book:
                    continue
                current = library.execute(f"SELECT value FROM {table} WHERE book=?", (book[0],)).fetchone()
            if not current:
                _run_calibredb(library_path, "set_custom", LABEL, str(book[0]), str(parse_page_count(pages)))
            else:
                pages = parse_page_count(current[0])
            app.execute(
                "INSERT INTO cwa_print_page_state(book_uuid,pages,updated_at) VALUES (?,?,?)",
                (book_uuid, pages, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")),
            )
            app.commit()
