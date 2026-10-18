from types import SimpleNamespace

import pytest
from fakes import NOW, FakeReddit, load
from prawcore.exceptions import Forbidden, NotFound, ServerError, TooManyRequests

from reader.collect import (
    Hasher,
    RunStats,
    backfill,
    collect_comments,
    collect_posts,
    reset_dead,
    run_collect,
    top_comments,
)
from reader.config import Subreddit
from reader.ratelimit import BudgetExhausted

SUB = Subreddit("testsub", 1, "test")
RESP = SimpleNamespace(status_code=404)
DAY = 86400


def ids(store, table):
    return sorted(r["id"] for r in store.conn.execute(f"SELECT id FROM {table}"))


def test_author_hashing(hasher):
    h = hasher("some_user")
    assert h == hasher("some_user")
    assert h != Hasher("other-salt")("some_user")
    assert h != hasher("another_user")
    assert "some_user" not in h and len(h) == 64
    assert hasher(None) is None


def test_posts_store_hash_not_username(store, hasher):
    collect_posts(FakeReddit(posts=load("posts.json")), store, SUB, hasher, NOW)
    row = store.conn.execute("SELECT * FROM posts WHERE id='p3'").fetchone()
    assert row["author_hash"] == hasher("user_3")
    assert "user_3" not in " ".join(str(v) for v in tuple(row))
    assert row["flair"] == "Question" and row["subreddit"] == "testsub"


def test_incremental_collection_by_last_seen(store, hasher):
    posts = load("posts.json")
    old, newest = posts[1:], posts[0]
    assert collect_posts(FakeReddit(posts=old), store, SUB, hasher, NOW) == 2  # p1 is removed
    assert store.get_state("testsub")["last_seen_id"] == "p3"
    assert ids(store, "posts") == ["p2", "p3"]
    assert collect_posts(FakeReddit(posts=posts), store, SUB, hasher, NOW + 60) == 1
    assert store.get_state("testsub")["last_seen_id"] == "p4"
    assert ids(store, "posts") == ["p2", "p3", "p4"]
    assert collect_posts(FakeReddit(posts=posts), store, SUB, hasher, NOW + 120) == 0
    assert newest["id"] == "p4"


def test_comment_depth_and_count_limits():
    sub = FakeReddit(comments=load("comments.json")).submission("p3")
    top = top_comments(sub, 10)
    # c6 is at depth 3 and c4 is removed, so neither is kept
    assert [c.id for c in top] == ["c1", "c3", "c5", "c2", "c7"]
    assert [c.id for c in top_comments(sub, 2)] == ["c1", "c3"]
    assert sub.comment_sort == "top"
    assert sub.comments.replace_more_limit == 0


def test_comments_only_for_posts_at_least_a_day_old(store, hasher):
    reddit = FakeReddit(posts=load("posts.json"), comments=load("comments.json"))
    collect_posts(reddit, store, SUB, hasher, NOW)
    added = collect_comments(reddit, store, SUB, hasher, NOW, top_n=3)
    assert added == 3  # only p3 (30h) and p2 (50h, no comments); p4 is 1h old
    assert ids(store, "comments") == ["c1", "c3", "c5"]
    row = store.conn.execute("SELECT * FROM comments WHERE id='c5'").fetchone()
    assert row["parent_id"] == "t1_c1" and row["post_id"] == "p3"
    assert collect_comments(reddit, store, SUB, hasher, NOW, top_n=3) == 0  # fetched once


def test_tier3_collects_posts_only(store, hasher, stats):
    sub = Subreddit("testsub", 3, "test")
    reddit = FakeReddit(posts=load("posts.json"), comments=load("comments.json"))
    run_collect(reddit, store, [sub], [3], hasher, stats, NOW)
    assert stats.posts == 3 and stats.comments == 0 and ids(store, "comments") == []


def test_schedule_skips_subreddits_that_are_not_due(store, hasher, stats):
    reddit = FakeReddit(posts=load("posts.json"))
    run_collect(reddit, store, [SUB], [1], hasher, stats, NOW)
    first = reddit.requests
    run_collect(reddit, store, [SUB], [1], hasher, stats, NOW + 3600)
    assert reddit.requests == first
    run_collect(reddit, store, [SUB], [1], hasher, stats, NOW + 3600, force=True)
    assert reddit.requests > first


def test_tier1_runs_before_tier2(store, hasher, stats):
    order = []

    class Spy(FakeReddit):
        def subreddit(self, name):
            order.append(name)
            return super().subreddit(name)

    subs = [Subreddit("two", 2, ""), Subreddit("one", 1, "")]
    run_collect(Spy(), store, subs, [2, 1], hasher, stats, NOW)
    assert order == ["one", "two"]


def test_dead_subreddit_after_five_failures(store, hasher, stats):
    reddit = FakeReddit(errors={"testsub": NotFound(RESP)})
    for n in range(1, 5):
        run_collect(reddit, store, [SUB], [1], hasher, stats, NOW + n, force=True)
        assert store.get_state("testsub")["dead_since"] is None
    run_collect(reddit, store, [SUB], [1], hasher, stats, NOW + 5, force=True)
    state = store.get_state("testsub")
    assert state["fail_count"] == 5 and state["dead_since"] == NOW + 5
    assert [r["name"] for r in store.dead_subreddits()] == ["testsub"]

    calls = reddit.requests
    run_collect(reddit, store, [SUB], [1], hasher, stats, NOW + 6, force=True)
    assert reddit.requests == calls  # dead subreddits are skipped

    reset_dead(store, "testsub")
    assert store.get_state("testsub")["dead_since"] is None
    run_collect(reddit, store, [SUB], [1], hasher, stats, NOW + 7, force=True)
    assert reddit.requests == calls + 1


def test_success_resets_failure_count(store, hasher, stats):
    bad = FakeReddit(errors={"testsub": NotFound(RESP)})
    for n in range(3):
        run_collect(bad, store, [SUB], [1], hasher, stats, NOW + n, force=True)
    run_collect(
        FakeReddit(posts=load("posts.json")), store, [SUB], [1], hasher, stats, NOW + 10, force=True
    )
    assert store.get_state("testsub")["fail_count"] == 0


def test_transient_errors_do_not_count_toward_dead(store, hasher, stats):
    reddit = FakeReddit(errors={"testsub": ServerError(RESP)})
    for n in range(6):
        run_collect(reddit, store, [SUB], [1], hasher, stats, NOW + n, force=True)
    assert store.get_state("testsub") is None  # transient errors leave no failure record


def test_budget_exhaustion_stops_cleanly_and_resumes(store, hasher, stats):
    class Limited(FakeReddit):
        def subreddit(self, name):
            if name == "second":
                raise BudgetExhausted("limit")
            return super().subreddit(name)

    subs = [SUB, Subreddit("second", 1, "")]
    run_collect(Limited(posts=load("posts.json")), store, subs, [1], hasher, stats, NOW)
    assert stats.status == "budget_exhausted"
    assert store.get_state("testsub")["last_collected_at"] == NOW
    assert store.get_state("second") is None  # untouched, so the next run picks it up

    # the next run (not forced) skips the finished subreddit and resumes with the other
    reddit = FakeReddit()
    reddit.posts["second"] = [
        SimpleNamespace(**dict(p, id="s" + p["id"])) for p in load("posts.json")
    ]
    again = RunStats(id=0)
    run_collect(reddit, store, subs, [1], hasher, again, NOW + 60)
    assert again.status == "ok" and again.posts == 3
    assert reddit.requests == 1  # only the listing of "second"
    assert store.get_state("second")["last_collected_at"] == NOW + 60
    assert store.counts()["posts"] == 6  # no duplicates


def test_upsert_idempotent_and_updates_score(store, hasher):
    posts = load("posts.json")
    reddit = FakeReddit(posts=posts)
    collect_posts(reddit, store, SUB, hasher, NOW)
    store.update_state("testsub", last_seen_id=None, last_seen_utc=None)
    posts[1]["score"] = 99
    collect_posts(FakeReddit(posts=posts), store, SUB, hasher, NOW + 100)
    assert store.counts() == {"comments": 0, "posts": 3}
    row = store.conn.execute("SELECT score, retrieved_at FROM posts WHERE id='p3'").fetchone()
    assert (row["score"], row["retrieved_at"]) == (99, NOW + 100)


def test_comment_upsert_idempotent(store, hasher):
    reddit = FakeReddit(posts=load("posts.json"), comments=load("comments.json"))
    collect_posts(reddit, store, SUB, hasher, NOW)
    store.conn.execute("UPDATE posts SET comments_fetched_at=NULL")
    collect_comments(reddit, store, SUB, hasher, NOW, 10)
    store.conn.execute("UPDATE posts SET comments_fetched_at=NULL")
    collect_comments(reddit, store, SUB, hasher, NOW, 10)
    assert store.counts()["comments"] == 5


@pytest.mark.parametrize("age_days,expected", [(8, 0), (2, 2)])
def test_old_posts_are_not_fetched_for_comments(store, hasher, age_days, expected):
    posts = [dict(load("posts.json")[1], created_utc=NOW - age_days * DAY)]
    posts[0]["id"] = "p3"
    reddit = FakeReddit(posts=posts, comments=load("comments.json"))
    collect_posts(reddit, store, SUB, hasher, NOW)
    assert collect_comments(reddit, store, SUB, hasher, NOW, top_n=2) == expected


def test_too_many_requests_stops_the_whole_run(store, hasher, stats):
    rate_limited = SimpleNamespace(status_code=429, headers={}, text="")
    reddit = FakeReddit(errors={"testsub": TooManyRequests(rate_limited)})
    subs = [SUB, Subreddit("second", 1, "")]
    run_collect(reddit, store, subs, [1], hasher, stats, NOW)
    assert reddit.requests == 1  # no further subreddits after a 429
    assert stats.status == "budget_exhausted"
    assert store.get_state("second") is None


def test_a_stored_post_seen_again_as_removed_is_deleted(store, hasher, stats):
    posts = load("posts.json")
    collect_posts(FakeReddit(posts=posts), store, SUB, hasher, NOW)
    posts[1].update(selftext="[deleted]", removed_by_category="deleted")  # p3
    backfill(FakeReddit(posts=posts), store, SUB, hasher, stats, NOW + 60)
    assert ids(store, "posts") == ["p2", "p4"]


def test_first_collection_reads_posts_only(store, hasher, stats):
    reddit = FakeReddit(posts=load("posts.json"), comments=load("comments.json"))
    run_collect(reddit, store, [SUB], [1], hasher, stats, NOW)
    assert stats.posts == 3 and ids(store, "comments") == []
    run_collect(reddit, store, [SUB], [1], hasher, stats, NOW + 6 * 3600)
    assert ids(store, "comments") != []  # later runs fetch comments as usual


def test_first_collection_reads_at_most_1000_posts(store, hasher, stats):
    template = load("posts.json")[0]
    posts = [dict(template, id=f"n{i}", created_utc=NOW - i) for i in range(1500)]
    reddit = FakeReddit(posts=posts)
    run_collect(reddit, store, [SUB], [1], hasher, stats, NOW)
    assert stats.posts == 1000 and reddit.requests == 10  # ten pages of 100


def test_dead_subreddit_content_is_purged_after_7_days(store, hasher, stats, caplog):
    collect_posts(FakeReddit(posts=load("posts.json")), store, SUB, hasher, NOW)
    store.update_state("testsub", dead_since=NOW, fail_count=5)
    run_collect(FakeReddit(), store, [SUB], [1], hasher, stats, NOW + 6 * DAY)
    assert store.counts()["posts"] == 3
    run_collect(FakeReddit(), store, [SUB], [1], hasher, stats, NOW + 7 * DAY)
    assert store.counts()["posts"] == 0
    assert "purged stored content" in caplog.text


def forbidden():
    return Forbidden(SimpleNamespace(status_code=403))


def test_five_subreddits_answering_403_is_a_sitewide_block(store, hasher, stats):
    subs = [Subreddit(f"s{i}", 1, "") for i in range(7)]
    reddit = FakeReddit(errors={s.name: forbidden() for s in subs})
    run_collect(reddit, store, subs, [1], hasher, stats, NOW)
    assert stats.status == "blocked"
    assert reddit.requests == 5  # stopped at the fifth 403
    assert "403" in store.get_block()["reason"]
    assert store.dead_subreddits() == []
    assert all(store.get_state(s.name) is None for s in subs)  # no failure recorded


def test_account_check_runs_before_marking_dead(store, hasher, stats):
    store.update_state("testsub", fail_count=4)
    reddit = FakeReddit(errors={"testsub": NotFound(RESP)}, me_error=forbidden())
    run_collect(reddit, store, [SUB], [1], hasher, stats, NOW)
    assert reddit.me_calls == 1 and stats.status == "blocked"
    assert "/api/v1/me" in store.get_block()["reason"]
    assert store.dead_subreddits() == [] and store.get_state("testsub")["fail_count"] == 4
