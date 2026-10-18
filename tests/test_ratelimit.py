import pytest

from reader.ratelimit import BudgetExhausted, RequestMeter, TokenBucket, utc_day_start


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept += seconds
        self.t += seconds


def bucket(rate, capacity):
    clock = Clock()
    return TokenBucket(rate, capacity, clock.now, clock.sleep), clock


def test_burst_then_steady_rate():
    b, clock = bucket(rate=2.0, capacity=4)
    for _ in range(4):
        b.acquire()
    assert clock.slept == 0
    for _ in range(4):
        b.acquire()
    assert clock.slept == pytest.approx(2.0)  # 4 more requests at 2 per second


def test_tokens_refill_but_cap_at_capacity():
    b, clock = bucket(rate=1.0, capacity=3)
    for _ in range(3):
        b.acquire()
    clock.t += 100
    for _ in range(3):
        b.acquire()
    assert clock.slept == 0
    b.acquire()
    assert clock.slept == pytest.approx(1.0)


def test_daily_budget_stops_and_counts_persist(store):
    run = store.start_run("t", "", 1_800_000_000)
    b, _ = bucket(rate=1000.0, capacity=1000)
    meter = RequestMeter(store, run, limit=3, bucket=b, clock=lambda: 1_800_000_000)
    for _ in range(3):
        meter.before_request()
    with pytest.raises(BudgetExhausted):
        meter.before_request()
    assert store.run_requests(run) == 3

    # a later run on the same day starts with the budget already used
    run2 = store.start_run("t", "", 1_800_000_100)
    again = RequestMeter(store, run2, limit=3, bucket=b, clock=lambda: 1_800_000_100)
    with pytest.raises(BudgetExhausted):
        again.before_request()


def test_budget_is_a_rolling_24_hour_window(store):
    now = 1_800_000_000
    run = store.start_run("t", "", now - 30 * 3600)
    store.add_request(run, now - 25 * 3600)  # outside the window
    store.add_request(run, now - 23 * 3600)  # inside, though the run started earlier
    store.add_request(run, now - 60)
    meter = RequestMeter(store, store.start_run("t", "", now), 3, clock=lambda: now)
    assert meter.used_last_24h == 2
    # just after midnight UTC, the requests of the evening before still count
    midnight = utc_day_start(now) + 86400
    late = RequestMeter(store, run, 3, clock=lambda: midnight + 60)
    store.add_request(run, midnight - 60)
    assert late.used_last_24h == 2


def test_runs_at_the_same_time_share_one_budget(store):
    now = 1_800_000_000
    b, _ = bucket(rate=1000.0, capacity=1000)
    first, second = (
        RequestMeter(store, store.start_run("t", "", now), limit=3, bucket=b, clock=lambda: now)
        for _ in range(2)
    )
    first.before_request()
    first.before_request()
    second.before_request()
    with pytest.raises(BudgetExhausted):
        first.before_request()
    assert store.requests_since(now - 86400) == 3
