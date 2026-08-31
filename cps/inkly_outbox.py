# -*- coding: utf-8 -*-
"""Durable local queue and scheduled delivery for Inkly events."""

import email.utils
import errno
import os
from datetime import datetime, timedelta, timezone

import requests
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.sql.expression import and_, or_

from . import logger, ub

log = logger.create()

OUTBOX_STATUS_PENDING = "pending"
OUTBOX_STATUS_DELIVERED = "delivered"
OUTBOX_STATUS_TERMINAL = "terminal"
OUTBOX_STATUS_AUTH_FAILED = "auth_failed"

RETRY_BACKOFF_SECONDS = (5, 30, 5 * 60, 30 * 60, 2 * 60 * 60, 6 * 60 * 60)
MAX_DELIVERY_BATCH = 25


def utc_now():
    return datetime.now(timezone.utc)


class InklyOutboxEvent(ub.Base):
    """One immutable Inkly event plus mutable delivery bookkeeping."""

    __tablename__ = "inkly_outbox_event"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False, index=True)
    event_id = Column(String(200), nullable=False, unique=True, index=True)
    event_type = Column(String(40), nullable=False)
    payload = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    attempt_count = Column(Integer, default=0, nullable=False)
    next_attempt_at = Column(DateTime, default=utc_now, nullable=True)
    last_attempt_at = Column(DateTime, nullable=True)
    last_error = Column(String(255), nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    status = Column(String(20), default=OUTBOX_STATUS_PENDING, nullable=False)
    terminal_error = Column(Boolean, default=False, nullable=False)

    __table_args__ = (
        Index("ix_inkly_outbox_due", "status", "next_attempt_at", "created_at"),
        Index("ix_inkly_outbox_user_created", "user_id", "created_at"),
    )

    def __repr__(self):
        return f"<InklyOutboxEvent id={self.id} event_id={self.event_id!r} status={self.status!r}>"


class InklyMetadataSync(ub.Base):
    """Last metadata fingerprint queued for a user/book/Inkly connection."""

    __tablename__ = "inkly_metadata_sync"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    book_uuid = Column(String(200), nullable=False)
    connection_key = Column(String(64), nullable=False)
    fingerprint = Column(String(64), nullable=False)
    last_queued_at = Column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "book_uuid", "connection_key", name="uq_inkly_metadata_sync_connection_book"),
        Index("ix_inkly_metadata_sync_user_book", "user_id", "book_uuid"),
    )


def ensure_tables(engine):
    """Create integration tables for an existing app.db when necessary."""
    InklyOutboxEvent.__table__.create(bind=engine, checkfirst=True)
    InklyMetadataSync.__table__.create(bind=engine, checkfirst=True)


def _new_session():
    return ub.get_new_session_instance()


def enqueue_events(user, events, metadata_event=None, metadata_fingerprint=None,
                   connection_key=None, book_uuid=None, _session=None):
    """Atomically enqueue events and, when needed, the current metadata event.

    A separate app.db session is used by default so an auxiliary outbox error
    cannot roll back the Kobo state transaction that already completed.
    """
    if not events and not metadata_event:
        return True

    own_session = _session is None
    session = _session or _new_session()
    user_id = int(user.id if hasattr(user, "id") else user)
    try:
        if metadata_event and metadata_fingerprint and connection_key and book_uuid:
            metadata_state = session.query(InklyMetadataSync).filter(
                InklyMetadataSync.user_id == user_id,
                InklyMetadataSync.book_uuid == str(book_uuid),
                InklyMetadataSync.connection_key == connection_key,
            ).first()
            if metadata_state is None or metadata_state.fingerprint != metadata_fingerprint:
                _add_event_if_missing(session, user_id, metadata_event)
                if metadata_state is None:
                    metadata_state = InklyMetadataSync(
                        user_id=user_id,
                        book_uuid=str(book_uuid),
                        connection_key=connection_key,
                        fingerprint=metadata_fingerprint,
                        last_queued_at=utc_now(),
                    )
                    session.add(metadata_state)
                else:
                    metadata_state.fingerprint = metadata_fingerprint
                    metadata_state.last_queued_at = utc_now()

        for event in events:
            _add_event_if_missing(session, user_id, event)
        session.commit()
        return True
    except Exception:
        session.rollback()
        log.error("Failed to enqueue Inkly event(s) for user %s; Kobo processing will continue", user_id)
        return False
    finally:
        if own_session:
            try:
                session.close()
            except Exception:
                pass


def _add_event_if_missing(session, user_id, event):
    event_id = str(event.get("eventId", ""))
    if not event_id:
        raise ValueError("Inkly event is missing eventId")
    existing = session.query(InklyOutboxEvent.id).filter(
        InklyOutboxEvent.event_id == event_id
    ).first()
    if existing is not None:
        return
    session.add(InklyOutboxEvent(
        user_id=user_id,
        event_id=event_id,
        event_type=str(event.get("eventType", "")),
        payload=event,
        created_at=utc_now(),
        next_attempt_at=utc_now(),
        status=OUTBOX_STATUS_PENDING,
        terminal_error=False,
    ))


def requeue_auth_failed_events(user_id, _session=None):
    """Make 401 rows due again after a user replaces or re-enables credentials."""
    own_session = _session is None
    session = _session or _new_session()
    try:
        count = session.query(InklyOutboxEvent).filter(
            InklyOutboxEvent.user_id == int(user_id),
            or_(
                InklyOutboxEvent.status == OUTBOX_STATUS_AUTH_FAILED,
                and_(
                    InklyOutboxEvent.status == OUTBOX_STATUS_PENDING,
                    InklyOutboxEvent.last_error == "configuration_missing",
                ),
            ),
        ).update({
            InklyOutboxEvent.status: OUTBOX_STATUS_PENDING,
            InklyOutboxEvent.terminal_error: False,
            InklyOutboxEvent.last_error: None,
            InklyOutboxEvent.next_attempt_at: utc_now(),
        }, synchronize_session=False)
        if own_session:
            session.commit()
        return count
    except Exception:
        session.rollback()
        return 0
    finally:
        if own_session:
            try:
                session.close()
            except Exception:
                pass


def _retry_after_seconds(value, now):
    if value is None:
        return None
    text = str(value).strip()
    try:
        seconds = int(text)
        return max(0, seconds)
    except ValueError:
        pass
    try:
        retry_at = email.utils.parsedate_to_datetime(text)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0, int((retry_at.astimezone(timezone.utc) - now).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return None


def retry_delay(attempt_count, retry_after=None, now=None):
    """Return the server-requested delay or the bounded exponential backoff."""
    now = now or utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    requested = _retry_after_seconds(retry_after, now)
    if requested is not None:
        return requested
    index = max(0, min(int(attempt_count) - 1, len(RETRY_BACKOFF_SECONDS) - 1))
    return RETRY_BACKOFF_SECONDS[index]


def _book_uuid(payload):
    if isinstance(payload, dict) and isinstance(payload.get("book"), dict):
        return payload["book"].get("uuid")
    return None


def _log_delivery_failure(event, category, status_code=None):
    suffix = f" status={status_code}" if status_code is not None else ""
    log.warning(
        "Inkly delivery failed: event=%s type=%s book=%s attempt=%s category=%s%s",
        event.event_id,
        event.event_type,
        _book_uuid(event.payload),
        event.attempt_count,
        category,
        suffix,
    )


def _finish_response(event, response, now):
    status_code = int(response.status_code)
    if 200 <= status_code < 300:
        event.status = OUTBOX_STATUS_DELIVERED
        event.terminal_error = False
        event.delivered_at = now
        event.next_attempt_at = None
        event.last_error = None
        return "delivered"

    retry_after = None
    try:
        retry_after = response.headers.get("Retry-After")
    except AttributeError:
        pass

    if status_code == 401:
        event.status = OUTBOX_STATUS_AUTH_FAILED
        event.terminal_error = True
        event.next_attempt_at = None
        event.last_error = "authentication_failed"
        return "auth_failed"

    if status_code == 429:
        event.status = OUTBOX_STATUS_PENDING
        event.terminal_error = False
        event.last_error = "http_429"
        event.next_attempt_at = now + timedelta(
            seconds=retry_delay(event.attempt_count, retry_after, now)
        )
        return "retry"

    if status_code == 400 or status_code == 413 or 400 <= status_code < 500:
        event.status = OUTBOX_STATUS_TERMINAL
        event.terminal_error = True
        event.next_attempt_at = None
        event.last_error = f"http_{status_code}"
        return "terminal"

    if status_code >= 500:
        event.status = OUTBOX_STATUS_PENDING
        event.terminal_error = False
        event.last_error = f"http_{status_code}"
        event.next_attempt_at = now + timedelta(
            seconds=retry_delay(event.attempt_count, retry_after, now)
        )
        return "retry"

    event.status = OUTBOX_STATUS_TERMINAL
    event.terminal_error = True
    event.next_attempt_at = None
    event.last_error = f"http_{status_code}"
    return "terminal"


def _finish_exception(event, category, now):
    event.status = OUTBOX_STATUS_PENDING
    event.terminal_error = False
    event.last_error = category
    event.next_attempt_at = now + timedelta(seconds=retry_delay(event.attempt_count, now=now))


def _close_http_responses(*responses):
    """Close each distinct response without letting cleanup mask delivery state."""
    closed = set()
    for response in responses:
        if response is None or id(response) in closed:
            continue
        closed.add(id(response))
        try:
            response.close()
        except Exception:
            log.warning("Failed to close an Inkly HTTP response")


def _is_emfile(error):
    """Return whether an exception chain contains an EMFILE failure."""
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if getattr(current, "errno", None) == errno.EMFILE:
            return True
        for attribute in ("__cause__", "__context__", "reason"):
            nested = getattr(current, attribute, None)
            if isinstance(nested, BaseException):
                pending.append(nested)
        pending.extend(item for item in getattr(current, "args", ()) if isinstance(item, BaseException))
    return False


def _fd_diagnostics():
    """Return non-sensitive process FD usage and limits when available."""
    open_fds = None
    soft_limit = None
    hard_limit = None
    try:
        open_fds = len(os.listdir("/proc/self/fd"))
    except OSError:
        pass
    try:
        import resource
        soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (ImportError, OSError, ValueError):
        pass
    return open_fds, soft_limit, hard_limit


def _log_emfile_diagnostics(error):
    if not _is_emfile(error):
        return
    open_fds, soft_limit, hard_limit = _fd_diagnostics()
    log.error(
        "Inkly delivery encountered EMFILE: open_fds=%s nofile_soft=%s nofile_hard=%s",
        open_fds if open_fds is not None else "unavailable",
        soft_limit if soft_limit is not None else "unavailable",
        hard_limit if hard_limit is not None else "unavailable",
    )


def deliver_due_inkly_events(limit=MAX_DELIVERY_BATCH, now=None, _session=None):
    """Deliver due rows through the existing scheduled-task infrastructure."""
    from .services import inkly

    now = now or utc_now()
    own_session = _session is None
    session = None
    result = {"delivered": 0, "retried": 0, "terminal": 0, "auth_failed": 0, "skipped": 0}
    try:
        session = _session if _session is not None else _new_session()
        events = session.query(InklyOutboxEvent).filter(
            InklyOutboxEvent.status == OUTBOX_STATUS_PENDING,
            or_(InklyOutboxEvent.next_attempt_at.is_(None), InklyOutboxEvent.next_attempt_at <= now),
        ).order_by(InklyOutboxEvent.created_at, InklyOutboxEvent.id).limit(int(limit)).all()

        for event in events:
            response = None
            exception_response = None
            delivery_error = None
            status_code = None
            user = session.query(ub.User).filter(ub.User.id == event.user_id).first()
            user_config = inkly.get_inkly_config(user) if user else None
            if not user_config:
                event.last_error = "configuration_missing"
                event.next_attempt_at = now + timedelta(seconds=RETRY_BACKOFF_SECONDS[-1])
                session.commit()
                result["skipped"] += 1
                continue

            # Reserve the row before doing network I/O. A later event is still
            # allowed to proceed if this one fails.
            event.attempt_count = (event.attempt_count or 0) + 1
            event.last_attempt_at = now
            event.next_attempt_at = now + timedelta(seconds=RETRY_BACKOFF_SECONDS[-1])
            payload = event.payload
            session.commit()

            try:
                response = inkly.send_inkly_event(user_config, payload)
                status_code = getattr(response, "status_code", None)
                outcome = _finish_response(event, response, now)
            except requests.exceptions.Timeout as error:
                delivery_error = error
                exception_response = getattr(error, "response", None)
                outcome = "retry"
                _finish_exception(event, "network_timeout", now)
            except requests.exceptions.RequestException as error:
                delivery_error = error
                exception_response = getattr(error, "response", None)
                outcome = "retry"
                _finish_exception(event, "network_error", now)
            except Exception as error:
                delivery_error = error
                exception_response = getattr(error, "response", None)
                outcome = "retry"
                _finish_exception(event, "delivery_error", now)
            finally:
                # _finish_response must inspect status/Retry-After first, but
                # every returned or exception-attached response is released
                # before DB bookkeeping or the next event in the batch.
                _close_http_responses(response, exception_response)

            if delivery_error is not None:
                _log_emfile_diagnostics(delivery_error)

            if outcome in ("retry", "terminal", "auth_failed"):
                _log_delivery_failure(event, event.last_error, status_code)
            session.commit()
            result["retried" if outcome == "retry" else outcome] += 1
        return result
    except Exception as error:
        if session is not None:
            session.rollback()
        _log_emfile_diagnostics(error)
        log.error("Inkly outbox worker failed; queued events remain inspectable")
        return result
    finally:
        if own_session and session is not None:
            try:
                session.close()
            except Exception:
                pass
