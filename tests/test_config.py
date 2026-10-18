import os
import stat

import pytest

from reader import config
from reader.config import (
    ConfigError,
    is_due,
    load_credentials,
    load_salt,
    load_subreddits,
)

HOUR = 3600


def write(tmp_path, text):
    p = tmp_path / "subs.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_load_subreddits(tmp_path):
    p = write(tmp_path, "subreddits:\n  - {name: A, tier: 1, field: x}\n  - {name: B, tier: 3}\n")
    subs = load_subreddits(p)
    assert [(s.name, s.tier, s.field) for s in subs] == [("A", 1, "x"), ("B", 3, "")]


def test_shipped_config_is_valid():
    subs = load_subreddits("subreddits.yaml")
    assert {s.tier for s in subs} == {1, 2, 3}


@pytest.mark.parametrize(
    "text",
    [
        "subreddits: []",
        "subreddits:\n  - {name: A, tier: 4}",
        "subreddits:\n  - {name: A, tier: 1}\n  - {name: a, tier: 2}",
        "subreddits:\n  - {tier: 1}",
    ],
)
def test_invalid_config(tmp_path, text):
    with pytest.raises(ConfigError):
        load_subreddits(write(tmp_path, text))


def test_tier_scheduling():
    now = 1_000_000.0
    assert is_due(1, None, now)
    assert not is_due(1, now - 3 * HOUR, now)
    assert is_due(1, now - 6 * HOUR, now)
    assert is_due(1, now - 6 * HOUR + 60, now)  # timer firing a minute early
    assert not is_due(2, now - 23 * HOUR + 600, now)
    assert is_due(2, now - 24 * HOUR, now)
    assert not is_due(3, now - 6 * 24 * HOUR, now)
    assert is_due(3, now - 7 * 24 * HOUR, now)


def test_credentials_password_and_refresh():
    base = {"REDDIT_CLIENT_ID": "i", "REDDIT_CLIENT_SECRET": "s", "REDDIT_USERNAME": "someone"}
    creds = load_credentials({**base, "REDDIT_PASSWORD": "p"})
    assert creds.user_agent == "linux:painmap-reddit-reader:v0.1 (by /u/someone)"
    assert load_credentials({**base, "REDDIT_REFRESH_TOKEN": "t"}).refresh_token == "t"


def test_credentials_missing_fails_clearly():
    with pytest.raises(ConfigError, match="REDDIT_USERNAME"):
        load_credentials(
            {"REDDIT_CLIENT_ID": "i", "REDDIT_CLIENT_SECRET": "s", "REDDIT_PASSWORD": "p"}
        )
    with pytest.raises(ConfigError, match="REDDIT_PASSWORD"):
        load_credentials(
            {"REDDIT_CLIENT_ID": "i", "REDDIT_CLIENT_SECRET": "s", "REDDIT_USERNAME": "u"}
        )


def test_salt_from_env():
    assert load_salt({"READER_AUTHOR_SALT": "abc"}) == "abc"


def test_salt_generated_once_in_separate_file(tmp_path):
    db = tmp_path / "data" / "reader.db"
    env = {"READER_SALT_FILE": str(tmp_path / "secret" / "salt")}
    first = load_salt(env, db)
    assert len(first) >= 32
    assert load_salt(env, db) == first
    if os.name != "nt":
        mode = stat.S_IMODE((tmp_path / "secret" / "salt").stat().st_mode)
        assert mode == 0o600


def test_salt_not_next_to_database(tmp_path):
    with pytest.raises(ConfigError):
        load_salt({"READER_SALT_FILE": str(tmp_path / "salt")}, tmp_path / "reader.db")


def test_salt_not_in_a_subfolder_of_the_database_folder(tmp_path):
    with pytest.raises(ConfigError):
        load_salt({"READER_SALT_FILE": str(tmp_path / "keys" / "salt")}, tmp_path / "reader.db")


def test_salt_written_by_another_run_first_is_kept(tmp_path, monkeypatch):
    salt_file = tmp_path / "secret" / "salt"

    def other_run_wins(nbytes):
        salt_file.write_text("other-run-salt", encoding="utf-8")
        return "this-run-salt"

    monkeypatch.setattr(config.secrets, "token_hex", other_run_wins)
    salt = load_salt({"READER_SALT_FILE": str(salt_file)}, tmp_path / "data" / "reader.db")
    assert salt == "other-run-salt"
    assert salt_file.read_text(encoding="utf-8").strip() == "other-run-salt"
