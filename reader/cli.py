"""Command line interface: collect, backfill, recheck, status, reset-dead, reset-block, auth."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from . import collect, recheck
from .auth import DEFAULT_REDIRECT, run_auth
from .client import make_reddit
from .config import (
    DAILY_BUDGET,
    DEFAULT_DB,
    ConfigError,
    load_credentials,
    load_salt,
    load_subreddits,
)
from .ratelimit import DAY, Interrupted, RequestMeter
from .store import Store

log = logging.getLogger("reader")
RUN_COMMANDS = {"collect", "backfill", "recheck"}  # the commands that talk to Reddit


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "msg": record.getMessage(),
            **getattr(record, "data", {}),
        }
        return json.dumps(entry)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    log.handlers[:] = [handler]
    log.setLevel(logging.INFO)


def fmt_time(ts) -> str:
    if ts is None:
        return "never"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


@contextmanager
def session(store: Store, kind: str, detail: str):
    """One recorded run: yields (reddit, stats, check) and logs the request numbers.

    On SIGTERM the run finishes its current item and is closed as "interrupted".
    """
    creds = load_credentials()
    now = time.time()
    stale = store.interrupt_stale_runs()
    if stale:
        log.warning("marked runs left running as interrupted", extra={"data": {"runs": stale}})
    run_id = store.start_run(kind, detail, now)
    stats = collect.RunStats(id=run_id)
    meter = RequestMeter(store, run_id, DAILY_BUDGET)
    stop_requested = False

    def on_sigterm(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    def check() -> None:
        if stop_requested:
            raise Interrupted("SIGTERM received")
        meter.check()

    previous = signal.signal(signal.SIGTERM, on_sigterm)
    try:
        yield make_reddit(creds, meter), stats, check
    except BaseException:
        stats.status = "error"
        raise
    finally:
        signal.signal(signal.SIGTERM, previous or signal.SIG_DFL)
        try:
            meter.observe()
        except Exception:  # the numbers are best effort; never hide the real error
            pass
        store.finish_run(
            run_id, time.time(), stats.status, stats.posts, stats.comments, stats.deleted
        )
        log.info(
            "run finished",
            extra={
                "data": {
                    "run": run_id,
                    "kind": kind,
                    "detail": detail,
                    "status": stats.status,
                    "requests": store.run_requests(run_id),
                    "requests_last_24h": meter.used_last_24h,
                    "budget_24h": DAILY_BUDGET,
                    "reddit_remaining": meter.reddit_remaining,
                    "reddit_used": meter.reddit_used,
                    "posts_added": stats.posts,
                    "comments_added": stats.comments,
                    "items_deleted": stats.deleted,
                }
            },
        )


def cmd_collect(args, store: Store) -> int:
    subs = load_subreddits(args.config)
    tiers = args.tier or [1, 2, 3]
    hasher = collect.Hasher(load_salt(db_path=args.db))
    with session(store, "collect", "tiers=" + ",".join(map(str, sorted(tiers)))) as (
        reddit,
        stats,
        check,
    ):
        collect.run_collect(
            reddit,
            store,
            subs,
            tiers,
            hasher,
            stats,
            time.time(),
            top_n=args.top_comments,
            force=args.force,
            check=check,
        )
    return 0


def cmd_backfill(args, store: Store) -> int:
    matches = [s for s in load_subreddits(args.config) if s.name.lower() == args.subreddit.lower()]
    if not matches:
        print(f"{args.subreddit} is not in {args.config}", file=sys.stderr)
        return 2
    hasher = collect.Hasher(load_salt(db_path=args.db))
    with session(store, "backfill", matches[0].name) as (reddit, stats, check):
        collect.backfill(reddit, store, matches[0], hasher, stats, time.time(), check)
    return 0


def cmd_recheck(args, store: Store) -> int:
    with session(store, "recheck", "") as (reddit, stats, check):
        recheck.run_recheck(reddit, store, stats, time.time(), check, DAILY_BUDGET)
    return 0


def cmd_auth(args, store) -> int:
    return run_auth(redirect_uri=args.redirect_uri)


def cmd_reset_dead(args, store: Store) -> int:
    state = store.get_state(args.subreddit)
    if not state or state["dead_since"] is None:
        print(f"{args.subreddit} is not marked dead", file=sys.stderr)
        return 2
    collect.reset_dead(store, args.subreddit)
    print(f"{args.subreddit} will be collected again")
    return 0


def cmd_reset_block(args, store: Store) -> int:
    if not store.get_block():
        print("no sitewide block is set", file=sys.stderr)
        return 2
    store.clear_block()
    print("sitewide block cleared; runs are allowed again")
    return 0


def cmd_status(args, store: Store) -> int:
    for table, n in store.counts().items():
        print(f"{table}: {n}")
    run = store.last_run()
    if run:
        print(
            f"last run: #{run['id']} {run['kind']} {run['detail']} at {fmt_time(run['started_at'])}"
            f" status={run['status']} requests={store.run_requests(run['id'])}"
        )
    else:
        print("last run: none")
    used = store.requests_since(time.time() - DAY)
    print(f"requests in the last 24 hours: {used} of {DAILY_BUDGET}")
    block = store.get_block()
    if block:
        print(f"SITEWIDE BLOCK since {fmt_time(block['since'])}: {block['reason']}")
    dead = store.dead_subreddits()
    print(f"dead subreddits: {len(dead)}")
    for row in dead:
        print(f"  {row['name']}: dead since {fmt_time(row['dead_since'])} ({row['last_error']})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reader", description="Read only Reddit collector.")
    p.add_argument("--db", default=os.environ.get("READER_DB", DEFAULT_DB), help="SQLite file")
    p.add_argument("--config", default="subreddits.yaml", help="subreddit list")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("collect", help="collect due subreddits")
    c.add_argument(
        "--tier",
        type=int,
        choices=[1, 2, 3],
        action="append",
        help="tier to collect (repeatable; default all, lowest first)",
    )
    c.add_argument("--top-comments", type=int, default=10, help="comments kept per post")
    c.add_argument("--force", action="store_true", help="ignore the schedule")
    c.set_defaults(func=cmd_collect)

    b = sub.add_parser("backfill", help="one time backfill of posts for one subreddit")
    b.add_argument("--subreddit", required=True)
    b.set_defaults(func=cmd_backfill)

    sub.add_parser("recheck", help="delete items removed on Reddit").set_defaults(func=cmd_recheck)
    sub.add_parser("status", help="counts, last run, dead subs, budget").set_defaults(
        func=cmd_status
    )

    a = sub.add_parser("auth", help="create a read only refresh token")
    a.add_argument("--redirect-uri", default=DEFAULT_REDIRECT, help="must match the app settings")
    a.set_defaults(func=cmd_auth)

    r = sub.add_parser("reset-dead", help="collect a dead subreddit again")
    r.add_argument("--subreddit", required=True)
    r.set_defaults(func=cmd_reset_dead)

    sub.add_parser("reset-block", help="allow runs again after a sitewide block").set_defaults(
        func=cmd_reset_block
    )
    return p


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = build_parser().parse_args(argv)
    store = None if args.command == "auth" else Store(args.db)  # auth needs no database
    try:
        block = store.get_block() if store and args.command in RUN_COMMANDS else None
        if block:
            print(
                f"error: sitewide block since {fmt_time(block['since'])} ({block['reason']}). "
                "Check the Reddit account and app, then run `reader reset-block`.",
                file=sys.stderr,
            )
            return 2
        return args.func(args, store)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    sys.exit(main())
