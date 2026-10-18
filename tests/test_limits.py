from types import SimpleNamespace

import pytest
from fakes import NOW, FakeHTTP, FakeReddit, load

from reader.client import make_reddit
from reader.collect import RunStats, run_collect
from reader.config import Credentials, Subreddit
from reader.ratelimit import BudgetExhausted, RequestMeter, TokenBucket

CREDS = Credentials("id", "secret", "someone", password=None, refresh_token="refresh")


def reddit_with_limits(remaining=None, used=None):
    return SimpleNamespace(auth=SimpleNamespace(limits={"remaining": remaining, "used": used}))


def meter_for(store, limit=5000, margin=100):
    run = store.start_run("t", "", NOW)
    bucket = TokenBucket(rate=1000.0, capacity=1000, sleep=lambda s: None)
    meter = RequestMeter(store, run, limit, bucket=bucket, clock=lambda: NOW, margin=margin)
    return meter, run


def test_stops_when_reddit_remaining_below_margin(store):
    meter, _ = meter_for(store)
    meter.attach(reddit_with_limits(remaining=101, used=899))
    meter.check()
    meter.attach(reddit_with_limits(remaining=99, used=901))
    with pytest.raises(BudgetExhausted, match="Reddit reports"):
        meter.check()
    assert (meter.reddit_remaining, meter.reddit_used) == (99, 901)


def test_stops_when_own_budget_reached_first(store):
    meter, _ = meter_for(store, limit=2)
    meter.attach(reddit_with_limits(remaining=900, used=2))
    meter.before_request()
    meter.check()
    meter.before_request()
    with pytest.raises(BudgetExhausted, match="per 24 hours"):
        meter.check()


def test_unset_limits_before_first_request_are_ignored(store):
    meter, _ = meter_for(store)
    meter.attach(reddit_with_limits())
    meter.check()  # all None: nothing to compare yet
    assert meter.used_last_24h == 0


def test_installed_praw_sends_every_request_through_the_meter(store, monkeypatch):
    """Fails if the installed PRAW no longer honours `requestor_class`."""
    monkeypatch.setattr("time.sleep", lambda s: None)  # prawcore waits before a retry
    http = FakeHTTP({"testsub": load("posts.json")}, fail_listing=1).install(monkeypatch)
    meter, run = meter_for(store)
    reddit = make_reddit(CREDS, meter)
    assert len(list(reddit.subreddit("testsub").new(limit=10))) == 4
    # the token request, the listing answered 503, and its retry
    assert http.calls == ["/api/v1/access_token", "/r/testsub/new", "/r/testsub/new"]
    assert store.run_requests(run) == 3
    meter.check()
    assert meter.reddit_remaining == 990


def test_meter_blocks_http_requests_past_the_budget(store, monkeypatch):
    http = FakeHTTP({"testsub": load("posts.json")}).install(monkeypatch)
    meter, _ = meter_for(store, limit=1)
    reddit = make_reddit(CREDS, meter)
    with pytest.raises(BudgetExhausted):
        list(reddit.subreddit("testsub").new(limit=10))
    assert http.calls == ["/api/v1/access_token"]  # the listing was never sent


def test_run_stops_cleanly_when_check_raises(store, hasher):
    stats = RunStats(id=store.start_run("t", "", NOW))
    sub = Subreddit("testsub", 1, "")
    seen = []

    def check():
        seen.append(1)
        if len(seen) > 2:
            raise BudgetExhausted("Reddit reports only 50 requests remaining")

    reddit = FakeReddit(posts=load("posts.json"))
    run_collect(reddit, store, [sub], [1], hasher, stats, NOW, check=check)
    assert stats.status == "budget_exhausted"
    assert store.get_state("testsub") is None  # nothing recorded, so the next run resumes
