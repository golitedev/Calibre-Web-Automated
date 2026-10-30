"""Library-wide print page counts, keyed by Calibre UUID in CWA's app.db."""

from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, DateTime, Integer, String

from . import ub


class PrintPageCount(ub.Base):
    __tablename__ = "cwa_print_page_count"
    book_uuid = Column(String(200), primary_key=True)
    pages = Column(Integer, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (CheckConstraint("pages >= 1 AND pages <= 100000"),)


def parse_page_count(value):
    value = "" if value is None else str(value).strip()
    if not value:
        return None
    if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 100000:
        raise ValueError("Print page count must be a whole number from 1 to 100000.")
    return int(value)


def get_page_count(book_uuid, session=None):
    session = session if session is not None else ub.session
    if session is None or not book_uuid:
        return None
    row = session.query(PrintPageCount).filter_by(book_uuid=book_uuid).first()
    return row.pages if row else None


def set_page_count(book_uuid, pages, session=None):
    session = session if session is not None else ub.session
    pages = parse_page_count(pages)
    row = session.query(PrintPageCount).filter_by(book_uuid=book_uuid).first()
    if pages is None:
        if row:
            session.delete(row)
    elif row:
        if row.pages != pages:
            row.pages = pages
            row.updated_at = datetime.now(timezone.utc)
    else:
        session.add(PrintPageCount(book_uuid=book_uuid, pages=pages))


def get_page_count_updated_at(book_uuid):
    if ub.session is None or not book_uuid:
        return None
    row = ub.session.query(PrintPageCount).filter_by(book_uuid=book_uuid).first()
    return row.updated_at.replace(tzinfo=timezone.utc).isoformat() if row else None
