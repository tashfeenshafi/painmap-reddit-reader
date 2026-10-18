import pytest
from fakes import NOW, FakeReddit, load

from reader.collect import collect_comments, collect_posts
from reader.config import Subreddit
from reader.ratelimit import BudgetExhausted
from reader.recheck import BATCH, RECHECK_SHARE, run_recheck

SUB = Subreddit("testsub", 1, "test")
DAY = 86400


def tier1_done(store, ts):
    run = store.start_run("collect", "tiers=1", ts)
    store.finish_run(run, ts + 60, "ok", 0, 0, 0)


@pytest.fixture(autouse=True)
def _tier1_done_today(store):
    tier1_done(store, NOW - 3600)


def seed(store, hasher):
    reddit = FakeReddit(posts=load("posts.json"), comments=load("comments.json"))
    collect_posts(reddit, store, SUB, hasher, NOW)
    collect_comments(reddit, store, SUB, hasher, NOW, 10)


def ids(store, table):
    return sorted(r["id"] for r in store.conn.execute(f"SELECT id FROM {table}"))


def add_posts(store, n):
    for i in range(n):
        store.upsert_post(
            {
                "id": f"x{i}",
                "subreddit": "testsub",
                "title": "t",
                "selftext": "",
                "author_hash": None,
                "created_utc": NOW,
                "score": 1,
                "num_comments": 0,
                "permalink": "",
                "flair": None,
                "retrieved_at": NOW,
            }
        )


def live(*item_ids):
    return [{"id": i, "selftext": "body", "removed_by_category": None} for i in item_ids]


def test_recheck_deletes_removed_items_and_their_text(store, hasher, stats):
    seed(store, hasher)
    reddit = FakeReddit(info=load("info.json"))
    run_recheck(reddit, store, stats, NOW)
    # p3 was deleted: the post and its stored comments go. p4 is missing only once: kept.
    assert ids(store, "posts") == ["p2", "p4"]
    assert ids(store, "comments") == []
    assert stats.deleted == 1
    text = " ".join(str(tuple(r)) for r in store.conn.execute("SELECT * FROM posts"))
    assert "Sample post 3" not in text


def test_recheck_keeps_live_items(store, hasher, stats):
    seed(store, hasher)
    run_recheck(FakeReddit(info=live("p2", "c1")), store, stats, NOW)
    assert ids(store, "posts") == ["p2", "p3", "p4"]
    assert "c1" in ids(store, "comments") and stats.deleted == 0


def test_recheck_ignores_items_over_30_days_old(store, hasher, stats):
    seed(store, hasher)
    later = NOW + 31 * DAY
    tier1_done(store, later)
    reddit = FakeReddit(info=load("info.json"))
    run_recheck(reddit, store, stats, later)
    assert reddit.requests == 0 and len(ids(store, "posts")) == 3


def test_recheck_batches_requests(store, stats):
    add_posts(store, BATCH + 5)
    reddit = FakeReddit()
    run_recheck(reddit, store, stats, NOW)
    assert [len(c) for c in reddit.info_calls] == [BATCH, 5]
    assert all(n.startswith("t3_") for c in reddit.info_calls for n in c)


def test_recheck_deletes_a_removed_comment_only(store, hasher, stats):
    seed(store, hasher)
    only_c3 = [i for i in load("info.json") if i["id"] == "c3"]
    run_recheck(FakeReddit(info=only_c3), store, stats, NOW)
    assert ids(store, "comments") == ["c1", "c2", "c5", "c7"]
    assert ids(store, "posts") == ["p2", "p3", "p4"] and stats.deleted == 1


def test_deleted_text_is_gone_from_the_files_on_disk(store, hasher, stats, tmp_path):
    seed(store, hasher)
    run_recheck(FakeReddit(info=load("info.json")), store, stats, NOW)
    on_disk = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.is_file())
    assert b"Fabricated body 3" not in on_disk  # includes the -wal file
    assert b"Fabricated comment 1" not in on_disk


def test_recheck_resumes_where_the_last_run_stopped(store, stats):
    add_posts(store, 3 * BATCH)
    seen: set[str] = set()
    for run in range(3):
        reddit = FakeReddit()

        def check(reddit=reddit):  # one batch per run, then the budget is spent
            if reddit.info_calls:
                raise BudgetExhausted("limit")

        run_recheck(reddit, store, stats, NOW + run, check)
        seen.update(name for call in reddit.info_calls for name in call)
    assert len(seen) == 3 * BATCH


def test_item_missing_twice_24_hours_apart_is_deleted(store, stats):
    add_posts(store, 3)  # x0 stays live, x1 and x2 disappear
    run_recheck(FakeReddit(info=live("x0")), store, stats, NOW)
    run_recheck(FakeReddit(info=live("x0", "x2")), store, stats, NOW + 3600)  # x2 is back
    assert ids(store, "posts") == ["x0", "x1", "x2"]  # an hour later is too soon
    tier1_done(store, NOW + DAY)
    run_recheck(FakeReddit(info=live("x0")), store, stats, NOW + DAY + 60)
    # x1 was first missed a day ago; x2 only now, since it came back in between
    assert ids(store, "posts") == ["x0", "x2"]


def test_a_whole_batch_missing_is_an_api_error(store, stats):
    add_posts(store, 3)
    for day in range(3):
        tier1_done(store, NOW + day * DAY)
        run_recheck(FakeReddit(), store, stats, NOW + day * DAY + 60)
    assert ids(store, "posts") == ["x0", "x1", "x2"] and stats.deleted == 0
    marks = store.conn.execute("SELECT missing_since FROM posts").fetchall()
    assert all(m[0] is None for m in marks)


def test_recheck_waits_for_the_first_tier1_run_of_the_day(store, stats):
    add_posts(store, 3)
    tomorrow = NOW + DAY
    reddit = FakeReddit()
    run_recheck(reddit, store, stats, tomorrow)
    assert reddit.requests == 0 and stats.status == "waiting"
    unfinished = store.start_run("collect", "tiers=1,2,3", tomorrow)  # still running
    run_recheck(reddit, store, stats, tomorrow + 60)
    assert reddit.requests == 0
    store.finish_run(unfinished, tomorrow + 120, "ok", 0, 0, 0)
    run_recheck(reddit, store, stats, tomorrow + 180)
    assert reddit.requests == 1


def test_recheck_uses_at_most_its_share_of_the_budget(store, stats):
    add_posts(store, 3 * BATCH)
    run = store.start_run("recheck", "", NOW - 3600)
    budget = 50
    for _ in range(int(budget * RECHECK_SHARE)):
        store.add_request(run, NOW - 3600)
    reddit = FakeReddit()
    run_recheck(reddit, store, stats, NOW, budget=budget)
    assert reddit.requests == 0 and stats.status == "budget_exhausted"
    # a day later the earlier requests are outside the rolling window
    tier1_done(store, NOW + DAY)
    run_recheck(reddit, store, stats, NOW + DAY, budget=budget)
    assert reddit.requests == 3
