"""End to end runs of `reader collect` through the CLI wiring, with real PRAW and fake HTTP."""

import signal

import pytest
from fakes import FakeHTTP, load

from reader import cli
from reader.store import Store

ENV = {
    "REDDIT_CLIENT_ID": "id",
    "REDDIT_CLIENT_SECRET": "secret",
    "REDDIT_USERNAME": "someone",
    "REDDIT_REFRESH_TOKEN": "refresh",
    "READER_AUTHOR_SALT": "test-salt",
}


@pytest.fixture
def paths(tmp_path, monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    config = tmp_path / "subs.yaml"
    config.write_text(
        "subreddits:\n  - {name: testsub, tier: 1}\n  - {name: other, tier: 1}\n",
        encoding="utf-8",
    )
    return tmp_path / "reader.db", config


def listings():
    other = [dict(p, id="o" + p["id"]) for p in load("posts.json")]
    return {"testsub": load("posts.json"), "other": other}


def collect(db, config):
    return cli.main(["--db", str(db), "--config", str(config), "collect", "--tier", "1"])


def test_collect_end_to_end(paths, monkeypatch):
    http = FakeHTTP(listings()).install(monkeypatch)
    db, config = paths
    assert collect(db, config) == 0
    store = Store(db)
    run = store.last_run()
    assert (run["kind"], run["detail"], run["status"]) == ("collect", "tiers=1", "ok")
    assert http.calls == ["/api/v1/access_token", "/r/testsub/new", "/r/other/new"]
    assert store.run_requests(run["id"]) == 3 and run["posts_added"] == 6
    assert store.counts() == {"comments": 0, "posts": 6}  # first run: posts only
    store.close()


def test_sigterm_finishes_the_current_item_and_closes_the_run(paths, monkeypatch):
    http = FakeHTTP(listings(), on_listing=lambda name: signal.raise_signal(signal.SIGTERM))
    http.install(monkeypatch)
    db, config = paths
    assert collect(db, config) == 0
    store = Store(db)
    run = store.last_run()
    assert run["status"] == "interrupted" and run["finished_at"] is not None
    assert "/r/other/new" not in http.calls  # stopped before the next subreddit
    store.close()
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL  # handler restored


def test_runs_left_running_are_marked_interrupted(paths, monkeypatch):
    FakeHTTP(listings()).install(monkeypatch)
    db, config = paths
    store = Store(db)
    stale = store.start_run("collect", "tiers=2", 1.0)  # the process died mid run
    store.close()
    assert collect(db, config) == 0
    store = Store(db)
    row = store.conn.execute("SELECT status FROM runs WHERE id=?", (stale,)).fetchone()
    assert row["status"] == "interrupted"
    store.close()


def test_runs_are_refused_while_blocked(paths, monkeypatch, capsys):
    http = FakeHTTP(listings()).install(monkeypatch)
    db, config = paths
    store = Store(db)
    store.set_block(1_800_000_000, "5 subreddits answered 403 in one run")
    store.close()
    assert collect(db, config) == 2
    assert "reset-block" in capsys.readouterr().err and http.calls == []
    assert cli.main(["--db", str(db), "status"]) == 0
    assert "SITEWIDE BLOCK" in capsys.readouterr().out
    assert cli.main(["--db", str(db), "reset-block"]) == 0
    assert collect(db, config) == 0 and http.calls


def test_recheck_end_to_end_sends_one_request_per_batch(paths, monkeypatch):
    db, config = paths
    FakeHTTP(listings()).install(monkeypatch)
    assert collect(db, config) == 0  # also completes today's tier 1 run
    info = [
        dict(p, removed_by_category="deleted", selftext="[deleted]") for p in load("posts.json")
    ]
    info[0] = load("posts.json")[0]  # p4 is still there
    http = FakeHTTP(listings(), info=info).install(monkeypatch)
    assert cli.main(["--db", str(db), "--config", str(config), "recheck"]) == 0
    assert http.calls == ["/api/v1/access_token", "/api/info"]  # no fetch per item
    store = Store(db)
    remaining = {r["id"] for r in store.conn.execute("SELECT id FROM posts")}
    assert remaining == {"p4", "op4", "op3", "op2"}  # other's posts were missing only once
    store.close()
