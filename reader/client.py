"""PRAW client setup. All Reddit traffic goes through the official API via PRAW."""

from __future__ import annotations

import praw
import prawcore

from .config import Credentials
from .ratelimit import RequestMeter


class MeteredRequestor(prawcore.Requestor):
    """Every HTTP request, including retries and token refreshes, passes the meter first.

    Installed through PRAW's documented `requestor_class` option.
    """

    def __init__(self, *, meter: RequestMeter, **kwargs):
        super().__init__(**kwargs)
        self.meter = meter

    def request(self, *args, **kwargs):
        self.meter.before_request()
        return super().request(*args, **kwargs)


def make_reddit(creds: Credentials, meter: RequestMeter) -> praw.Reddit:
    kwargs: dict = {
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "user_agent": creds.user_agent,
        "check_for_updates": False,  # no network calls other than the Reddit API
        "requestor_class": MeteredRequestor,
        "requestor_kwargs": {"meter": meter},
    }
    if creds.refresh_token:
        kwargs["refresh_token"] = creds.refresh_token
    else:
        kwargs["username"] = creds.username
        kwargs["password"] = creds.password
    reddit = praw.Reddit(**kwargs)
    # PRAW also sleeps according to the X-Ratelimit-* headers on its own.
    meter.attach(reddit)
    return reddit
