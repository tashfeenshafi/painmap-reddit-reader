from types import SimpleNamespace

import pytest

from reader import cli
from reader.auth import SCOPES, AuthError, parse_redirect, run_auth

ENV = {"REDDIT_CLIENT_ID": "id", "REDDIT_CLIENT_SECRET": "secret", "REDDIT_USERNAME": "someone"}


class FakeAuth:
    def __init__(self):
        self.url_args = None
        self.authorized_with = None

    def url(self, scopes, state, duration):
        self.url_args = (scopes, state, duration)
        return f"https://example.invalid/authorize?state={state}"

    def authorize(self, code):
        self.authorized_with = code
        return "fake-refresh-token"

    def scopes(self):
        return set(SCOPES)


def run(pasted_builder):
    auth, out = FakeAuth(), []

    def factory(**kwargs):
        factory.kwargs = kwargs
        return SimpleNamespace(auth=auth)

    code = run_auth(
        ENV,
        ask=lambda prompt: pasted_builder(auth.url_args[1]),
        say=out.append,
        factory=factory,
    )
    return code, auth, "\n".join(out), factory.kwargs


def test_only_read_and_identity_scopes_are_requested():
    assert SCOPES == ["identity", "read"]  # identity only for /api/v1/me
    code, auth, out, kwargs = run(lambda s: f"http://localhost:8080/?state={s}&code=abc123#_")
    assert code == 0
    assert auth.url_args[0] == ["identity", "read"] and auth.url_args[2] == "permanent"
    assert auth.authorized_with == "abc123"
    assert "REDDIT_REFRESH_TOKEN=fake-refresh-token" in out
    assert kwargs["user_agent"].endswith("(by /u/someone)")
    assert "password" not in kwargs


def test_state_mismatch_is_rejected():
    with pytest.raises(AuthError, match="state"):
        parse_redirect("http://localhost:8080/?state=wrong&code=abc", "right")


def test_error_and_missing_code_are_rejected():
    with pytest.raises(AuthError, match="access_denied"):
        parse_redirect("http://localhost:8080/?error=access_denied&state=s", "s")
    with pytest.raises(AuthError, match="no code"):
        parse_redirect("http://localhost:8080/?state=s", "s")


def test_bare_code_is_accepted():
    assert parse_redirect("  abc123#_ ", "s") == "abc123"


def test_auth_command_needs_no_database_or_login(tmp_path, monkeypatch, capsys):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("REDDIT_PASSWORD", raising=False)
    monkeypatch.setattr(cli, "run_auth", lambda redirect_uri: 0)
    db = tmp_path / "never-created.db"
    assert cli.main(["--db", str(db), "auth"]) == 0
    assert not db.exists()
