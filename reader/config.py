"""Subreddit list, tier schedule, and settings read from the environment."""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

HOUR = 3600
TIER_INTERVALS = {1: 6 * HOUR, 2: 24 * HOUR, 3: 7 * 24 * HOUR}  # seconds
TIER_SLACK = 300  # a timer firing a few minutes early still counts as due
COMMENT_TIERS = (1, 2)  # tier 3 collects posts only
DAILY_BUDGET = 5000
DEFAULT_DB = "reader.db"
DEFAULT_SALT_FILE = "~/.config/painmap-reader/author_salt"


class ConfigError(Exception):
    """Raised for missing or invalid configuration."""


@dataclass(frozen=True)
class Subreddit:
    name: str
    tier: int
    field: str


def load_subreddits(path: str | Path) -> list[Subreddit]:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    entries = (data or {}).get("subreddits")
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"{path}: expected a non-empty 'subreddits' list")
    subs: list[Subreddit] = []
    seen: set[str] = set()
    for entry in entries:
        try:
            name, tier = str(entry["name"]).strip(), int(entry["tier"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"{path}: bad entry {entry!r}") from exc
        if not name or tier not in TIER_INTERVALS:
            raise ConfigError(f"{path}: bad name or tier in {entry!r}")
        if name.lower() in seen:
            raise ConfigError(f"{path}: duplicate subreddit {name}")
        seen.add(name.lower())
        subs.append(Subreddit(name, tier, str(entry.get("field", ""))))
    return subs


def is_due(tier: int, last_collected_at: float | None, now: float) -> bool:
    if last_collected_at is None:
        return True
    return now - last_collected_at >= TIER_INTERVALS[tier] - TIER_SLACK


@dataclass(frozen=True)
class Credentials:
    client_id: str
    client_secret: str
    username: str
    password: str | None
    refresh_token: str | None

    @property
    def user_agent(self) -> str:
        return f"linux:painmap-reddit-reader:v0.1 (by /u/{self.username})"


def load_credentials(env: Mapping[str, str] | None = None, need_login: bool = True) -> Credentials:
    """Read credentials from the environment. `need_login=False` is for `reader auth`."""
    env = os.environ if env is None else env
    get = lambda key: (env.get(key) or "").strip()  # noqa: E731
    required = ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USERNAME"]
    missing = [key for key in required if not get(key)]
    if need_login and not get("REDDIT_REFRESH_TOKEN") and not get("REDDIT_PASSWORD"):
        missing.append("REDDIT_PASSWORD (or REDDIT_REFRESH_TOKEN)")
    if missing:
        raise ConfigError("missing environment variables: " + ", ".join(missing))
    return Credentials(
        client_id=get("REDDIT_CLIENT_ID"),
        client_secret=get("REDDIT_CLIENT_SECRET"),
        username=get("REDDIT_USERNAME"),
        password=get("REDDIT_PASSWORD") or None,
        refresh_token=get("REDDIT_REFRESH_TOKEN") or None,
    )


def load_salt(env: Mapping[str, str] | None = None, db_path: str | Path = DEFAULT_DB) -> str:
    """Return the author-hash salt.

    Taken from READER_AUTHOR_SALT if set. Otherwise generated once and stored in a
    separate file (READER_SALT_FILE, mode 600), never in the database or its folder.
    If two runs create it at the same moment, both use the one that was written first.
    """
    env = os.environ if env is None else env
    salt = (env.get("READER_AUTHOR_SALT") or "").strip()
    if salt:
        return salt
    salt_path = Path(env.get("READER_SALT_FILE") or DEFAULT_SALT_FILE).expanduser().resolve()
    if salt_path.is_relative_to(Path(db_path).expanduser().resolve().parent):
        raise ConfigError("the salt file must not be in the database folder or below it")
    if not salt_path.exists():
        salt_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = salt_path.with_name(f"{salt_path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(secrets.token_hex(32) + "\n")
            fh.flush()
            os.fsync(fh.fileno())  # stored hashes must never outlive their salt
        try:
            os.link(tmp, salt_path)  # fails if another run created it first; keep that one
        except FileExistsError:
            pass
        finally:
            tmp.unlink()
    salt = salt_path.read_text(encoding="utf-8").strip()
    if not salt:
        raise ConfigError(f"{salt_path} is empty; remove it to generate a new salt")
    return salt
