"""Daily recheck: delete stored posts and comments that were removed or deleted on Reddit."""

from __future__ import annotations

import logging

from .collect import RunStats, is_removed, noop
from .config import DAILY_BUDGET
from .ratelimit import DAY, BudgetExhausted, StopRun, utc_day_start

log = logging.getLogger("reader")

RECHECK_WINDOW = 30 * DAY
RECHECK_SHARE = 0.2  # of the 24 hour budget
MISSING_GRACE = DAY  # an item must be missing in two rechecks at least this far apart
BATCH = 100  # reddit.info() sends up to 100 fullnames per request
PREFIX = {"posts": "t3_", "comments": "t1_"}


def recheck_batch(reddit, store, table: str, batch: list[str], stats: RunStats, now: float):
    found = {item.id: item for item in reddit.info(fullnames=[PREFIX[table] + i for i in batch])}
    store.mark_rechecked(table, batch, now)
    if not found:  # a whole batch does not vanish at once: treat it as an API error
        log.warning("recheck: Reddit returned none of a batch; deleting nothing")
        return
    missing = [i for i in batch if i not in found]
    first_missed = store.missing_since(table, missing)
    store.set_missing(table, present=list(found), missing=missing, ts=now)
    gone = [i for i, item in found.items() if is_removed(item)]
    gone += [
        i
        for i in missing
        if first_missed.get(i) is not None and now - first_missed[i] >= MISSING_GRACE
    ]
    for item_id in gone:
        store.delete_item(table, item_id)
        stats.deleted += 1


def run_recheck(
    reddit, store, stats: RunStats, now: float, check=noop, budget: int = DAILY_BUDGET
) -> None:
    """One daily slice, least recently rechecked first, using at most RECHECK_SHARE of the
    budget. It waits until the day's first tier 1 collect run has completed."""
    if not store.tier1_completed_since(utc_day_start(now)):
        stats.status = "waiting"
        log.info("recheck waits until today's first tier 1 run has completed")
        return
    cap = int(budget * RECHECK_SHARE)
    try:
        for table in ("posts", "comments"):
            ids = store.ids_to_recheck(table, now - RECHECK_WINDOW)
            for i in range(0, len(ids), BATCH):
                check()
                if store.requests_since(now - DAY, kind="recheck") >= cap:
                    raise BudgetExhausted(f"recheck share of {cap} requests per 24 hours reached")
                recheck_batch(reddit, store, table, ids[i : i + BATCH], stats, now)
    except StopRun as exc:
        stats.status = exc.status
        log.warning("stopping: %s; the next recheck continues", exc)
    finally:
        store.purge_wal()
