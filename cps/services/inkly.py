# -*- coding: utf-8 -*-
"""Inkly event construction and transport.

This module deliberately contains no Hardcover lookups or business rules.
It turns local CWA/Calibre facts into Inkly's event contract and leaves
delivery to :mod:`cps.inkly_outbox`.
"""

import base64
import copy
import hashlib
import json
import math
import os
from datetime import date, datetime, timezone
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import requests

from .. import config, logger, ub

log = logger.create()

INKLY_EVENTS_PATH = "/api/integrations/cwa/events"
INKLY_REQUEST_TIMEOUT = (3, 10)
MAX_COVER_BASE64_LENGTH = 2_800_000
MAX_EVENT_BYTES = 3_800_000
MAX_ANNOTATIONS_PER_EVENT = 100
MAX_DELETED_IDS_PER_EVENT = 100
SUPPORTED_COVER_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def normalize_inkly_base_url(value):
    """Validate and normalize a user-provided Inkly base URL."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            raise ValueError("Inkly base URL must use http or https")
        # Accessing ``port`` makes urllib reject malformed/out-of-range ports
        # instead of deferring the failure to the background worker.
        _ = parsed.port
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Inkly base URL must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("Inkly base URL must not contain a query or fragment")
        path = parsed.path.rstrip("/")
        if path.endswith(INKLY_EVENTS_PATH):
            path = path[:-len(INKLY_EVENTS_PATH)].rstrip("/")
        return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("Invalid Inkly base URL") from error


def _normalize_token(value):
    token = str(value or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if "\r" in token or "\n" in token:
        raise ValueError("Inkly token contains invalid characters")
    return token


def get_inkly_config(user):
    """Return normalized credentials for an enabled user, or ``None``.

    The returned token is used only in memory by the HTTP worker. Callers must
    not log or render this dictionary.
    """
    if user is None or not bool(getattr(user, "inkly_enabled", False)):
        return None
    try:
        base_url = normalize_inkly_base_url(getattr(user, "inkly_base_url", ""))
        token = _normalize_token(getattr(user, "inkly_token", ""))
    except ValueError:
        return None
    if not base_url or not token:
        return None
    connection_key = hashlib.sha256(
        (base_url + "\0" + token).encode("utf-8")
    ).hexdigest()
    return {
        "base_url": base_url,
        "endpoint": base_url + INKLY_EVENTS_PATH,
        "token": token,
        "connection_key": connection_key,
    }


def has_inkly_config(user):
    return get_inkly_config(user) is not None


def apply_user_settings(user, form):
    """Apply the opt-in Inkly settings from a profile/admin form.

    An empty token input intentionally preserves the stored token. The token
    is only removed when the explicit clear checkbox is selected.
    """
    setting_keys = {"inkly_enabled", "inkly_base_url", "inkly_token", "inkly_clear_token"}
    if not setting_keys.intersection(form):
        return False

    old_values = (
        bool(getattr(user, "inkly_enabled", False)),
        getattr(user, "inkly_base_url", None),
        getattr(user, "inkly_token", None),
    )
    enabled = form.get("inkly_enabled") == "on"
    base_raw = str(form.get("inkly_base_url", "") or "").strip()
    base_url = normalize_inkly_base_url(base_raw) if base_raw else None

    token = getattr(user, "inkly_token", None)
    if form.get("inkly_clear_token") == "on":
        token = None
    elif str(form.get("inkly_token", "") or "").strip():
        token = _normalize_token(form.get("inkly_token"))

    if enabled and (not base_url or not token):
        raise ValueError("Inkly requires a valid base URL and token when enabled")

    user.inkly_enabled = enabled
    user.inkly_base_url = base_url
    user.inkly_token = token
    new_values = (enabled, base_url, token)
    changed = old_values != new_values
    if changed:
        try:
            from .. import inkly_outbox
            inkly_outbox.requeue_auth_failed_events(user.id, _session=ub.session)
        except Exception:
            # Settings persistence must not depend on an optional queue row.
            pass
    return changed


def _text(value, limit, default=None):
    if value is None:
        return default
    result = str(value).strip()
    if not result:
        return default
    return result[:limit]


def _first_value(values):
    for value in values:
        if value:
            return value
    return None


def _book_file(book):
    data_items = list(getattr(book, "data", []) or [])
    if not data_items:
        return None
    priority = {"kepub": 0, "epub": 1}
    data_items.sort(key=lambda item: priority.get(str(getattr(item, "format", "")).lower(), 2))
    data = data_items[0]
    file_name = str(getattr(data, "name", "") or "").strip()
    book_format = str(getattr(data, "format", "") or "").strip().lower()
    if file_name and book_format and not file_name.lower().endswith("." + book_format):
        file_name += "." + book_format
    return _text(file_name, 500)


def _identifiers(book):
    result = {}
    isbn_values = {}
    for identifier in list(getattr(book, "identifiers", []) or []):
        id_type = _text(getattr(identifier, "type", ""), 80)
        value = _text(getattr(identifier, "val", ""), 240)
        if not id_type or not value:
            continue
        normalized_type = id_type.lower().replace("-", "_")
        if normalized_type in ("isbn", "isbn10", "isbn_10", "isbn13", "isbn_13"):
            isbn_values.setdefault(normalized_type, value.replace("-", "").replace(" ", ""))

        # Inkly accepts at most 50 identifiers. Keep the first 50 in Calibre's
        # relationship order while still extracting a later ISBN field.
        if len(result) >= 50:
            continue
        key = id_type
        suffix = 2
        while key in result:
            suffix_text = "#" + str(suffix)
            key = id_type[:80 - len(suffix_text)] + suffix_text
            suffix += 1
        result[key] = value

    # Older Calibre databases can retain ISBN in the legacy books column
    # instead of the identifiers relationship.
    if not isbn_values:
        legacy_isbn = _text(getattr(book, "isbn", None), 240)
        if legacy_isbn:
            normalized_legacy_isbn = legacy_isbn.replace("-", "").replace(" ", "")
            isbn_values["isbn"] = normalized_legacy_isbn
            if len(result) < 50:
                result.setdefault("isbn", legacy_isbn)

    isbn = _first_value(isbn_values.get(key) for key in ("isbn", "isbn10", "isbn_10", "isbn13", "isbn_13"))
    isbn10 = _first_value(isbn_values.get(key) for key in ("isbn10", "isbn_10"))
    isbn13 = _first_value(isbn_values.get(key) for key in ("isbn13", "isbn_13"))
    if isbn and not isbn10 and len(isbn) == 10:
        isbn10 = isbn
    if isbn and not isbn13 and len(isbn) == 13:
        isbn13 = isbn
    return result, _text(isbn, 40), _text(isbn10, 40), _text(isbn13, 40)


def _total_pages(book):
    for attribute in ("total_pages", "page_count", "pages"):
        value = getattr(book, attribute, None)
        if value is None or isinstance(value, bool):
            continue
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            continue
        if numeric > 0 and numeric <= 100_000:
            return numeric
    return None


def _cover_candidates(book):
    if bool(getattr(config, "config_use_google_drive", False)):
        return []
    try:
        root = config.get_book_path()
        book_path = os.path.normpath(os.path.join(root, str(getattr(book, "path", "") or "")))
    except Exception:
        return []
    return [os.path.join(book_path, "cover" + extension) for extension in SUPPORTED_COVER_TYPES]


def _cover_payload(book):
    for cover_path in _cover_candidates(book):
        try:
            size = os.path.getsize(cover_path)
            encoded_size = 4 * math.ceil(size / 3)
            if encoded_size > MAX_COVER_BASE64_LENGTH:
                continue
            with open(cover_path, "rb") as cover_file:
                encoded = base64.b64encode(cover_file.read()).decode("ascii")
            if not encoded or len(encoded) > MAX_COVER_BASE64_LENGTH:
                continue
            mime_type = SUPPORTED_COVER_TYPES[os.path.splitext(cover_path)[1].lower()]
            return {"data": encoded, "mimeType": mime_type}
        except (OSError, IOError):
            continue
    return None


def _cover_signature(book):
    for cover_path in _cover_candidates(book):
        try:
            stat = os.stat(cover_path)
            return f"{cover_path}:{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            continue
    return ""


def build_inkly_book(book, include_cover=False):
    """Build the single shared Calibre/CWA -> Inkly book mapping."""
    raw_uuid = _text(getattr(book, "uuid", None), 200)
    if not raw_uuid:
        raw_uuid = "calibre-" + str(getattr(book, "id", "unknown"))

    authors = []
    for author in list(getattr(book, "authors", []) or []):
        name = _text(getattr(author, "name", author), 120)
        if name:
            authors.append(name)
        if len(authors) >= 12:
            break

    identifiers, isbn, isbn10, isbn13 = _identifiers(book)
    comments = list(getattr(book, "comments", []) or [])
    publishers = list(getattr(book, "publishers", []) or [])
    languages = list(getattr(book, "languages", []) or [])
    publication_date = getattr(book, "pubdate", None)
    if isinstance(publication_date, (datetime, date)):
        if publication_date == getattr(type(book), "DEFAULT_PUBDATE", None):
            publication_date = None
        else:
            publication_date = publication_date.isoformat()

    payload = {
        "uuid": raw_uuid,
        "calibreId": getattr(book, "id", None),
        "title": _text(getattr(book, "title", None), 180, "Unknown"),
        "authors": authors,
        "author": _text(authors[0] if authors else None, 120),
        "fileName": _book_file(book),
        "format": "ebook",
        "isbn": isbn,
        "isbn10": isbn10,
        "isbn13": isbn13,
        "identifiers": identifiers,
        "totalPages": _total_pages(book),
        "description": _text(getattr(comments[0], "text", None) if comments else None, 10_000),
        "publisher": _text(getattr(publishers[0], "name", None) if publishers else None, 500),
        "language": _text(
            _first_value([
                getattr(languages[0], "lang_code", None) if languages else None,
                getattr(languages[0], "language_name", None) if languages else None,
            ]),
            35,
        ),
        "publicationDate": _text(publication_date, 40),
    }
    if include_cover:
        cover = _cover_payload(book)
        if cover:
            payload["cover"] = cover
    return payload


def metadata_fingerprint(book):
    payload = build_inkly_book(book, include_cover=False)
    payload["coverSignature"] = _cover_signature(book)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _as_utc(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_utc_timestamp(value=None):
    parsed = _as_utc(value) or datetime.now(timezone.utc)
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def reading_state_timestamp(state, fallback=None):
    """Choose the newest valid Kobo LastModified timestamp in a state."""
    candidates = []

    def collect_last_modified(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() in ("lastmodified", "last_modified"):
                    candidates.append(child)
                collect_last_modified(child)
        elif isinstance(value, list):
            for child in value:
                collect_last_modified(child)

    collect_last_modified(state)
    parsed = [timestamp for timestamp in (_as_utc(value) for value in candidates) if timestamp]
    return format_utc_timestamp(max(parsed) if parsed else fallback)


def annotation_timestamp(annotations, fallback=None):
    candidates = []
    for annotation in annotations or []:
        if isinstance(annotation, dict):
            candidates.append(annotation.get("clientLastModifiedUtc"))
    parsed = [timestamp for timestamp in (_as_utc(value) for value in candidates) if timestamp]
    return format_utc_timestamp(max(parsed) if parsed else fallback)


def _event_fits(event):
    try:
        # requests serializes ``json=`` with JSON's default ASCII escaping and
        # whitespace. Mirror that conservative representation before enqueue.
        size = len(json.dumps(event).encode("utf-8"))
    except (OverflowError, TypeError, ValueError):
        return False
    return size <= MAX_EVENT_BYTES


def build_inkly_event(event_type, book, data=None, occurred_at=None, include_cover=False):
    return {
        "eventId": str(uuid4()),
        "eventType": event_type,
        "occurredAt": format_utc_timestamp(occurred_at),
        "book": build_inkly_book(book, include_cover=include_cover),
        "data": data if isinstance(data, dict) else {},
    }


def _metadata_event(user, book, occurred_at=None):
    return build_inkly_event("book.metadata", book, {}, occurred_at=occurred_at, include_cover=True)


def _enqueue_with_metadata(user, book, events):
    user_config = get_inkly_config(user)
    if not user_config:
        return False
    book_payload = build_inkly_book(book, include_cover=False)
    metadata_event = _metadata_event(user, book)
    safe_events = []
    for event in events or []:
        if _event_fits(event):
            safe_events.append(event)
        else:
            log.error(
                "Inkly event exceeds request limit: type=%s book=%s; event was not queued",
                event.get("eventType"),
                book_payload.get("uuid"),
            )
    if not _event_fits(metadata_event):
        log.error(
            "Inkly metadata event exceeds request limit: book=%s; metadata was not queued",
            book_payload.get("uuid"),
        )
        metadata_event = None
    if not safe_events and metadata_event is None:
        return False
    from .. import inkly_outbox
    return inkly_outbox.enqueue_events(
        user,
        safe_events,
        metadata_event=metadata_event,
        metadata_fingerprint=metadata_fingerprint(book),
        connection_key=user_config["connection_key"],
        book_uuid=book_payload["uuid"],
    )


def queue_reading_state(user, book, state):
    """Queue a raw Kobo reading state after CWA has persisted it."""
    if not get_inkly_config(user):
        return False
    raw_state = copy.deepcopy(state) if isinstance(state, dict) else {}
    event = build_inkly_event(
        "reading.state",
        book,
        {"state": raw_state},
        occurred_at=reading_state_timestamp(raw_state),
    )
    return _enqueue_with_metadata(user, book, [event])


def _annotation_for_payload(annotation):
    if not isinstance(annotation, dict):
        return None
    result = copy.deepcopy(annotation)
    annotation_id = _text(result.get("id"), 256)
    highlighted = _text(result.get("highlightedText"), 50_000)
    note = _text(result.get("noteText"), 50_000)
    if not annotation_id or (not highlighted and not note):
        return None
    result["id"] = annotation_id
    if highlighted is None:
        result.pop("highlightedText", None)
    else:
        result["highlightedText"] = highlighted
    if note is None:
        result.pop("noteText", None)
    else:
        result["noteText"] = note
    return result


def _annotation_batch_fits(book, updated, deleted, occurred_at):
    event = build_inkly_event(
        "annotations.sync",
        book,
        {"updatedAnnotations": updated, "deletedAnnotationIds": deleted},
        occurred_at=occurred_at,
    )
    return _event_fits(event)


def _annotation_batches(book, updated, deleted, occurred_at):
    """Chunk by Inkly's item limits and keep each JSON request below 4 MiB."""
    remaining_updated = list(updated)
    remaining_deleted = list(deleted)
    batches = []
    while remaining_updated or remaining_deleted:
        current_updated = []
        current_deleted = []
        while remaining_updated and len(current_updated) < MAX_ANNOTATIONS_PER_EVENT:
            candidate = current_updated + [remaining_updated[0]]
            if current_updated and not _annotation_batch_fits(book, candidate, current_deleted, occurred_at):
                break
            current_updated.append(remaining_updated.pop(0))
        while remaining_deleted and len(current_deleted) < MAX_DELETED_IDS_PER_EVENT:
            candidate = current_deleted + [remaining_deleted[0]]
            if current_updated or current_deleted:
                if not _annotation_batch_fits(book, current_updated, candidate, occurred_at):
                    break
            current_deleted.append(remaining_deleted.pop(0))
        if not current_updated and not current_deleted:
            # Known annotation text is bounded to 50,000 characters, so this
            # should only be reachable if the receiver contract changes.
            if remaining_updated:
                current_updated.append(remaining_updated.pop(0))
            elif remaining_deleted:
                current_deleted.append(remaining_deleted.pop(0))
        batches.append((current_updated, current_deleted))
    return batches


def queue_annotation_sync(user, book, updated_annotations, deleted_annotation_ids):
    """Queue structured annotation updates/deletes without flattening them."""
    if not get_inkly_config(user):
        return False
    updated = []
    for annotation in updated_annotations or []:
        prepared = _annotation_for_payload(annotation)
        if prepared is not None:
            updated.append(prepared)
    deleted = []
    for annotation_id in deleted_annotation_ids or []:
        value = _text(annotation_id, 256)
        if value:
            deleted.append(value)
    if not updated and not deleted:
        return False

    occurred_at = annotation_timestamp(updated, fallback=datetime.now(timezone.utc))
    events = []
    for batch_updated, batch_deleted in _annotation_batches(book, updated, deleted, occurred_at):
        events.append(build_inkly_event(
            "annotations.sync",
            book,
            {
                "updatedAnnotations": batch_updated,
                "deletedAnnotationIds": batch_deleted,
            },
            occurred_at=occurred_at,
        ))
    return _enqueue_with_metadata(user, book, events)


def queue_book_metadata_for_users(book):
    """Queue current metadata for users already linked to this book.

    A local metadata edit is a fan-out update, not a discovery mechanism. A
    user without an existing metadata/link row should first enter the
    integration through a Kobo reading or annotation sync; that path still
    creates the row and queues the initial metadata event via
    :func:`_enqueue_with_metadata`.
    """
    if book is None:
        return 0
    try:
        from ..inkly_outbox import InklyMetadataSync

        book_uuid = build_inkly_book(book, include_cover=False)["uuid"]
        users = (
            ub.session.query(ub.User)
            .join(InklyMetadataSync, InklyMetadataSync.user_id == ub.User.id)
            .filter(
                ub.User.inkly_enabled.is_(True),
                InklyMetadataSync.book_uuid == book_uuid,
            )
            .distinct()
            .all()
        )
    except Exception:
        log.error("Failed to find users for Inkly metadata synchronization")
        return 0

    queued = 0
    for user in users:
        try:
            if _enqueue_with_metadata(user, book, []):
                queued += 1
        except Exception:
            log.error("Failed to enqueue Inkly metadata for book %s", getattr(book, "id", None))
    return queued


def send_inkly_event(user_config, payload):
    """POST one event using the bounded timeout; the caller owns the response."""
    return requests.post(
        user_config["endpoint"],
        headers={
            "Authorization": "Bearer " + user_config["token"],
            "Content-Type": "application/json",
        },
        json=payload,
        allow_redirects=False,
        timeout=INKLY_REQUEST_TIMEOUT,
    )
