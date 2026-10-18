"""Client side request limiting: a token bucket, a rolling 24 hour budget, and Reddit's numbers."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone

from .store import Store

DAY = 86400
SAFETY_MARGIN = 100  # stop when Reddit reports fewer requests than this remaining in its window


class StopRun(Exception):
    """The run stops cleanly before the next item; the next run resumes."""

    status = "stopped"


class BudgetExhausted(StopRun):
    """A request limit was reached."""

    status = "budget_exhausted"


class Interrupted(StopRun):
    """The process was asked to stop (SIGTERM)."""

    status = "interrupted"


class TokenBucket:
    """Allows short bursts but a steady rate of at most `rate` requests per second."""

    def __init__(
        self,
        rate: float = 1.0,
        capacity: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.rate, self.capacity = rate, capacity
        self._clock, self._sleep = clock, sleep
        self._tokens = capacity
        self._last = clock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def acquire(self) -> None:
        self._refill()
        if self._tokens < 1:
            self._sleep((1 - self._tokens) / self.rate)
            self._refill()
        self._tokens -= 1


def utc_day_start(now: float) -> float:
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class RequestMeter:
    """Counts every HTTP request and stops the run at whichever limit is reached first.

    1. Our own budget: requests in the last 24 hours (a rolling window over the
       timestamps in the `requests` table), shared by every run.
    2. What Reddit reports through `reddit.auth.limits`: stop if fewer than `margin`
       requests remain in its current window.

    `client.MeteredRequestor` calls `before_request()` for every HTTP request PRAW makes,
    including retries and token refreshes.
    """

    def __init__(
        self,
        store: Store,
        run_id: int,
        limit: int,
        bucket: TokenBucket | None = None,
        clock: Callable[[], float] = time.time,
        margin: int = SAFETY_MARGIN,
    ):
        self.store, self.run_id, self.limit, self.margin = store, run_id, limit, margin
        self.bucket = bucket or TokenBucket()
        self.clock = clock
        self.reddit = None
        self.reddit_remaining: float | None = None
        self.reddit_used: int | None = None

    @property
    def used_last_24h(self) -> int:
        return self.store.requests_since(self.clock() - DAY)

    def attach(self, reddit) -> None:
        self.reddit = reddit

    def _raise_if_over_budget(self) -> None:
        if self.used_last_24h >= self.limit:
            raise BudgetExhausted(f"budget of {self.limit} requests per 24 hours reached")

    def before_request(self) -> None:
        self._raise_if_over_budget()
        self.bucket.acquire()
        self.store.add_request(self.run_id, self.clock())

    def observe(self) -> None:
        """Read Reddit's numbers.

        PRAW 8 reports only `remaining` and `used`, with no reset time. That is enough:
        the stop condition needs only `remaining`, and our own counting is per request.
        """
        if self.reddit is None:
            return
        limits = self.reddit.auth.limits
        self.reddit_remaining, self.reddit_used = limits.get("remaining"), limits.get("used")

    def check(self) -> None:
        """Observe, then raise BudgetExhausted if either limit is reached."""
        self.observe()
        self._raise_if_over_budget()
        if self.reddit_remaining is not None and self.reddit_remaining < self.margin:
            raise BudgetExhausted(
                f"Reddit reports only {self.reddit_remaining:.0f} requests remaining in its window"
            )
