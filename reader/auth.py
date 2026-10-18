"""`reader auth`: create a refresh token with only the read and identity scopes."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from urllib.parse import parse_qs, urlparse

import praw

from .config import ConfigError, load_credentials

# "read" to read posts and comments; "identity" only so /api/v1/me can confirm the
# account still works before a subreddit is marked dead. Neither can write anything.
SCOPES = ["identity", "read"]
DEFAULT_REDIRECT = "http://localhost:8080"


class AuthError(ConfigError):
    """The authorization step failed."""


def parse_redirect(text: str, state: str) -> str:
    """Return the authorization code from the pasted address (or a bare code)."""
    text = text.strip()
    if "?" not in text and "code=" not in text:
        return text.removesuffix("#_")
    query = parse_qs(urlparse(text).query)
    if "error" in query:
        raise AuthError(f"Reddit returned an error: {query['error'][0]}")
    if query.get("state", [None])[0] != state:
        raise AuthError("the state in the address does not match; start again")
    code = query.get("code", [""])[0].removesuffix("#_")
    if not code:
        raise AuthError("no code found in the address")
    return code


def run_auth(
    env: Mapping[str, str] | None = None,
    redirect_uri: str = DEFAULT_REDIRECT,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
    factory=praw.Reddit,
) -> int:
    creds = load_credentials(env, need_login=False)
    reddit = factory(
        client_id=creds.client_id,
        client_secret=creds.client_secret,
        redirect_uri=redirect_uri,
        user_agent=creds.user_agent,
        check_for_updates=False,
    )
    state = secrets.token_urlsafe(16)
    url = reddit.auth.url(scopes=SCOPES, state=state, duration="permanent")
    say(f"Your app's redirect uri on Reddit must be exactly: {redirect_uri}")
    say(f"Requested scopes: {', '.join(SCOPES)} (no write access)\n")
    say("1. Open this address in a browser where you are logged in as the Reddit account:")
    say(f"\n   {url}\n")
    say("2. Click Allow. The browser then goes to an address that will not load; that is fine.")
    say("3. Copy that full address from the address bar and paste it here.\n")
    code = parse_redirect(ask("Address: "), state)
    token = reddit.auth.authorize(code)
    granted = sorted(reddit.auth.scopes())
    say(f"\nGranted scopes: {', '.join(granted)}")
    say("\nPut this in your environment (it is a secret; do not commit it):\n")
    say(f"REDDIT_REFRESH_TOKEN={token}")
    return 0
