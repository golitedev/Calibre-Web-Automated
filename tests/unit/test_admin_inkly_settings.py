from types import SimpleNamespace

import pytest
from flask import Flask, get_flashed_messages

from cps import admin
from cps import inkly_outbox


INKLY_VALIDATION_MESSAGE = "Inkly requires a valid base URL and token when enabled"


class _Query:
    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return []

    def count(self):
        return 1


class _Session:
    def __init__(self):
        self.rollback_calls = 0

    def query(self, *args, **kwargs):
        return _Query()

    def rollback(self):
        self.rollback_calls += 1


def _user():
    return SimpleNamespace(
        id=7,
        inkly_enabled=False,
        inkly_base_url=None,
        inkly_token=None,
        theme=1,
        sidebar_view=0,
        kobo_only_shelves_sync=0,
        auto_send_enabled=False,
        auto_metadata_fetch=False,
        view_settings={},
        is_anonymous=True,
        default_language="all",
        locale="en",
        role=0,
        email="user@example.com",
        name="test",
        kindle_mail="",
        kindle_mail_subject="",
    )


def _app():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(admin.admi)
    return app


def _configure_handler(monkeypatch):
    session = _Session()
    commit_calls = []

    monkeypatch.setattr(admin.ub, "session", session)
    monkeypatch.setattr(admin.ub, "session_commit", lambda *args, **kwargs: commit_calls.append(True) or "")
    monkeypatch.setattr(admin, "get_sidebar_config", lambda *args, **kwargs: ([], None))
    monkeypatch.setattr(admin, "flag_modified", lambda *args, **kwargs: None)
    monkeypatch.setattr(admin, "_", lambda message, **kwargs: message % kwargs if kwargs else message)
    monkeypatch.setattr(inkly_outbox, "requeue_auth_failed_events", lambda *args, **kwargs: None)
    return session, commit_calls


def _form(user, **inkly_settings):
    return {
        "email": user.email,
        "name": user.name,
        "kindle_mail": "",
        "kindle_mail_subject": "",
        **inkly_settings,
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "form",
    [
        {"inkly_enabled": "on", "inkly_token": "token-one"},
        {"inkly_enabled": "on", "inkly_base_url": "https://inkly.example"},
    ],
    ids=["missing-base-url", "missing-token"],
)
def test_invalid_admin_inkly_settings_redirect_with_validation_message(form, monkeypatch):
    user = _user()
    session, _ = _configure_handler(monkeypatch)

    with _app().test_request_context():
        response = admin._handle_edit_user(_form(user, **form), user, [], {}, False)

        assert response.status_code == 302
        assert response.headers["Location"] == "/admin/user/7"
        assert get_flashed_messages(with_categories=True) == [
            ("error", INKLY_VALIDATION_MESSAGE),
        ]

    assert session.rollback_calls == 1
    assert user.inkly_enabled is False
    assert user.inkly_base_url is None
    assert user.inkly_token is None


@pytest.mark.unit
def test_valid_admin_inkly_settings_are_applied(monkeypatch):
    user = _user()
    session, commit_calls = _configure_handler(monkeypatch)

    with _app().test_request_context():
        response = admin._handle_edit_user(
            _form(
                user,
                inkly_enabled="on",
                inkly_base_url="https://inkly.example/",
                inkly_token="Bearer token-one",
            ),
            user,
            [],
            {},
            False,
        )

    assert response == ""
    assert session.rollback_calls == 0
    assert len(commit_calls) == 1
    assert user.inkly_enabled is True
    assert user.inkly_base_url == "https://inkly.example"
    assert user.inkly_token == "token-one"
