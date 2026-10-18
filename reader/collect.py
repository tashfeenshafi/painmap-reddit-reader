"""Collect new posts and top comments, and run the one time backfill."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass

from prawcore.exceptions import (
    Forbidden,
    NotFound,
    PrawcoreException,
    Redirect,
    TooManyRequests,
)

from .config import COMMENT_TIERS, Subreddit, is_due
from .ratelimit import BudgetExhausted, StopRun

log = logging.getLogger("reader")

DAY = 86400
DEAD_AFTER_FAILURES = 5
DEAD_PURGE_AFTER = 7 * DAY  # stored content of a dead subreddit is deleted after this
SITEWIDE_403 = 5  # this many subreddits answering 403 in one run means a sitewide block
COMMENT_MIN_AGE = DAY  # comments are fetched once a post is at least 24 hours old
COMMENT_MAX_AGE = 7 * DAY  # ...and only while it is still fairly recent
MAX_COMMENT_DEPTH = 2
LISTING_LIMIT = 1000  # Reddit's listings end around here anyway
BACKFILL_WINDOW = 365 * DAY
REMOVED_TEXT = {"[removed]", "[deleted]"}
# A private, banned, or missing subreddit shows up as one of these.
UNAVAILABLE = (NotFound, Forbidden, Redirect)


class SitewideBlock(StopRun):
    """Reddit seems to block the account or app as a whole, not single subreddits."""

    status = "blocked"


def noop() -> None:
    """Default for the `check` callbacks: the limit check run between API calls."""


@dataclass
class RunStats:
    id: int
    posts: int = 0
    comments: int = 0
    deleted: int = 0
    status: str = "ok"


class Hasher:
    """SHA256 of a username with a local salt. The raw username is never stored."""

    def __init__(self, salt: str):
        self._salt = salt

    def __call__(self, username) -> str | None:
        if not username:
            return None
        return hashlib.sha256(f"{self._salt}:{username}".encode()).hexdigest()


def is_removed(item) -> bool:
    """True for a post or comment that was removed or deleted on Reddit.

    Reads only data already loaded: asking a PRAW object for a missing attribute
    (like `body` on a post) would make PRAW fetch the whole item again.
    """
    data = vars(item)
    if data.get("removed_by_category"):
        return True
    return data.get("body", data.get("selftext", "")) in REMOVED_TEXT


def post_row(s, subreddit: str, hasher: Hasher, now: float) -> dict:
    return {
        "id": s.id,
        "subreddit": subreddit,
        "title": s.title,
        "selftext": s.selftext,
        "author_hash": hasher(s.author),
        "created_utc": s.created_utc,
        "score": s.score,
        "num_comments": s.num_comments,
        "permalink": s.permalink,
        "flair": s.link_flair_text,
        "retrieved_at": now,
    }


def comment_row(c, post_id: str, subreddit: str, hasher: Hasher, now: float) -> dict:
    return {
        "id": c.id,
        "post_id": post_id,
        "parent_id": c.parent_id,
        "subreddit": subreddit,
        "body": c.body,
        "author_hash": hasher(c.author),
        "created_utc": c.created_utc,
        "score": c.score,
        "permalink": c.permalink,
        "retrieved_at": now,
    }


def collect_posts(
    reddit, store, sub: Subreddit, hasher: Hasher, now: float, check: Callable = noop
) -> int:
    """Store posts newer than the last seen one. Re-seen posts are simply updated.

    The first time, this reads the whole listing: up to about 1,000 recent posts.
    """
    state = store.get_state(sub.name)
    last_id = state["last_seen_id"] if state else None
    last_utc = state["last_seen_utc"] if state else None
    newest = None
    added = 0
    for s in reddit.subreddit(sub.name).new(limit=LISTING_LIMIT):
        check()
        if last_utc is not None and (s.id == last_id or s.created_utc < last_utc):
            break
        if newest is None:
            newest = (s.id, s.created_utc)
        if is_removed(s):
            store.delete_item("posts", s.id)  # in case an earlier run stored it
            continue
        store.upsert_post(post_row(s, sub.name, hasher, now))
        added += 1
    if newest:
        store.update_state(sub.name, last_seen_id=newest[0], last_seen_utc=newest[1])
    return added


def top_comments(submission, limit: int) -> list:
    """Top `limit` comments by score, looking at top level comments and their replies only."""
    found: list = []

    def walk(comments, depth: int) -> None:
        for c in comments:
            if is_removed(c):
                continue
            found.append(c)
            if depth < MAX_COMMENT_DEPTH:
                walk(c.replies, depth + 1)

    submission.comment_sort = "top"
    submission.comments.replace_more(limit=0)  # drop "load more" stubs without extra requests
    walk(submission.comments, 1)
    found.sort(key=lambda c: (-c.score, c.id))
    return found[:limit]


def collect_comments(
    reddit,
    store,
    sub: Subreddit,
    hasher: Hasher,
    now: float,
    top_n: int,
    check: Callable = noop,
) -> int:
    added = 0
    for post_id in store.posts_needing_comments(
        sub.name, older_than=now - COMMENT_MIN_AGE, newer_than=now - COMMENT_MAX_AGE
    ):
        check()
        submission = reddit.submission(id=post_id)
        rows = [
            comment_row(c, post_id, sub.name, hasher, now) for c in top_comments(submission, top_n)
        ]
        store.upsert_comments(rows)
        store.mark_comments_fetched(post_id, now)
        added += len(rows)
    return added


def _check_account(reddit) -> None:
    """Call /api/v1/me. If even that fails, the problem is not the subreddit."""
    try:
        reddit.user.me()
    except PrawcoreException as exc:
        raise SitewideBlock(f"/api/v1/me failed ({type(exc).__name__})") from exc


def _record_failures(reddit, store, failures: dict[str, Exception], now: float) -> None:
    """Count failures. Subreddits reaching the limit are marked dead only once
    /api/v1/me shows that the account itself still works."""
    counts = {}
    for name, exc in failures.items():
        state = store.get_state(name)
        counts[name] = ((state["fail_count"] if state else 0) + 1, type(exc).__name__)
    dying = [name for name, (fails, _) in counts.items() if fails >= DEAD_AFTER_FAILURES]
    if dying:
        _check_account(reddit)
    for name, (fails, error) in counts.items():
        if name in dying:
            store.update_state(name, fail_count=fails, last_error=error, dead_since=now)
            log.error("subreddit marked dead", extra={"data": {"subreddit": name, "error": error}})
        else:
            store.update_state(name, fail_count=fails, last_error=error)
            log.warning(
                "subreddit unavailable", extra={"data": {"subreddit": name, "fails": fails}}
            )


def _stop(store, stats: RunStats, exc: StopRun, now: float) -> None:
    stats.status = exc.status
    if isinstance(exc, SitewideBlock):
        store.set_block(now, str(exc))
        log.error(
            "sitewide block: %s. No subreddit was marked dead. Runs are refused until "
            "`reader reset-block` is run.",
            exc,
        )
    else:
        log.warning("stopping: %s; next run resumes", exc)


def purge_dead(store, stats: RunStats, now: float) -> None:
    """Delete the stored content of subreddits that have been dead for DEAD_PURGE_AFTER."""
    purged = False
    for row in store.dead_subreddits():
        if now - row["dead_since"] >= DEAD_PURGE_AFTER:
            posts, comments = store.purge_subreddit(row["name"])
            if posts or comments:
                purged = True
                stats.deleted += posts + comments
                log.warning(
                    "purged stored content of a subreddit dead for 7 days",
                    extra={
                        "data": {"subreddit": row["name"], "posts": posts, "comments": comments}
                    },
                )
    if purged:
        store.purge_wal()


def reset_dead(store, name: str) -> None:
    store.update_state(name, fail_count=0, dead_since=None, last_error=None)


def run_collect(
    reddit,
    store,
    subs: list[Subreddit],
    tiers: list[int],
    hasher: Hasher,
    stats: RunStats,
    now: float,
    top_n: int = 10,
    force: bool = False,
    check: Callable = noop,
) -> None:
    """Collect due subreddits, lowest tier number first so tier 1 gets budget priority.

    A subreddit's first collection reads posts only; comments follow in later runs.
    """
    purge_dead(store, stats, now)
    failures: dict[str, Exception] = {}
    try:
        try:
            for tier in sorted(tiers):
                for sub in (s for s in subs if s.tier == tier):
                    check()
                    state = store.get_state(sub.name)
                    if state and state["dead_since"] is not None:
                        log.info("skipping dead subreddit", extra={"data": {"subreddit": sub.name}})
                        continue
                    last = state["last_collected_at"] if state else None
                    if not force and not is_due(tier, last, now):
                        continue
                    first = state is None or state["last_seen_utc"] is None
                    try:
                        stats.posts += collect_posts(reddit, store, sub, hasher, now, check)
                        if tier in COMMENT_TIERS and not first:
                            stats.comments += collect_comments(
                                reddit, store, sub, hasher, now, top_n, check
                            )
                    except UNAVAILABLE as exc:
                        failures[sub.name] = exc
                        if sum(isinstance(e, Forbidden) for e in failures.values()) >= SITEWIDE_403:
                            raise SitewideBlock(
                                f"{SITEWIDE_403} subreddits answered 403 in one run"
                            ) from exc
                        continue
                    except TooManyRequests as exc:  # Reddit says slow down: stop the whole run
                        raise BudgetExhausted("Reddit answered 429 Too Many Requests") from exc
                    except PrawcoreException as exc:  # transient: try again next run
                        log.warning(
                            "request failed",
                            extra={"data": {"subreddit": sub.name, "error": type(exc).__name__}},
                        )
                        continue
                    store.update_state(
                        sub.name, last_collected_at=now, fail_count=0, last_error=None
                    )
        except SitewideBlock:
            raise
        except StopRun:
            _record_failures(reddit, store, failures, now)
            raise
        _record_failures(reddit, store, failures, now)
    except StopRun as exc:
        _stop(store, stats, exc, now)


def backfill(
    reddit,
    store,
    sub: Subreddit,
    hasher: Hasher,
    stats: RunStats,
    now: float,
    check: Callable = noop,
) -> None:
    """Posts only, up to one year back, as far as the listing goes (about 1,000 items)."""
    cutoff = now - BACKFILL_WINDOW
    newest = None
    try:
        try:
            for s in reddit.subreddit(sub.name).new(limit=LISTING_LIMIT):
                check()
                if s.created_utc < cutoff:
                    break
                if newest is None:
                    newest = (s.id, s.created_utc)
                if is_removed(s):
                    store.delete_item("posts", s.id)
                else:
                    store.upsert_post(post_row(s, sub.name, hasher, now))
                    stats.posts += 1
        except UNAVAILABLE as exc:
            _record_failures(reddit, store, {sub.name: exc}, now)
    except StopRun as exc:
        _stop(store, stats, exc, now)
    state = store.get_state(sub.name)
    if newest and (
        not state or state["last_seen_utc"] is None or newest[1] > state["last_seen_utc"]
    ):
        store.update_state(sub.name, last_seen_id=newest[0], last_seen_utc=newest[1])
