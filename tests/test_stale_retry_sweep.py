"""Stale-reservation cleanup before a retry's budget check (issue A).

A request that crashed leaves a reservation behind. Before this fix, a *retry* of a live request was
checked against the budget while that dead reservation was still counted, so it could be refused for no
good reason (the sweep only ran when a brand-new request started). `start_attempt` now sweeps first.

Every test runs in both budget modes. Providers are fakes, time is a fake clock, prices are made up.
Nothing here calls a real API or spends anything. Money is whole microdollars.
"""
from datetime import datetime, timezone

import pytest

import config
import generateImage
import limits
import services
import usage
from conftest import ScriptedProvider, make_http_error
from errors import LimitExceededError
from test_budget_modes import configure
from test_limits import HF, Clock, attempt, begin_images, img, run_threads
from test_usage import prompt_data
from usage import SqliteUsageStore

UTC = timezone.utc
PRICE = 450_000
MODES = ["cumulative", "monthly"]


@pytest.fixture
def clock():
    return Clock(datetime(2026, 10, 15, 12, 0, tzinfo=UTC))  # mid-month: nowhere near a month boundary


@pytest.fixture
def store(tmp_path, clock):
    return SqliteUsageStore(tmp_path / "retry_sweep.db", clock=clock, stale_after_seconds=900)


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def window_summary(store):
    """What the budget check sees, using exactly the window the real check uses."""
    return store.cost_summary("huggingface", limits.budget_quota(config.get_settings(), "huggingface"))


def crashed_x_then_live_r2_with_a_failed_first_attempt(store, clock):
    """X reserves $0.90 and its process dies. 800 s later (X not yet stale) R2 is authorised and its
    first attempt fails, so a retry will be needed."""
    x = begin_images(store, 2)
    clock.advance(800)
    r2 = begin_images(store, 1)
    first = attempt(store, r2, item=1, price=PRICE)
    store.finish_attempt(first, usage.ATTEMPT_FAILED, error_category="server_error")
    return x, r2


# ------------------------------------------------------------------ the fix itself
@pytest.mark.parametrize("mode", MODES)
def test_a_retry_is_not_refused_because_of_a_crashed_requests_stale_reservation(store, clock, settings, mode):
    configure(settings, mode, "1.5", "0.45", margin=0)  # $1.50: room for X's $0.90 + R2's $0.45, not for a retry too
    x, r2 = crashed_x_then_live_r2_with_a_failed_first_attempt(store, clock)
    clock.advance(200)  # X is now 1000 s silent (stale); R2 has only been silent for 200 s (alive)
    assert store.get_request(x)["outcome"] == usage.PENDING  # nobody has swept X yet: it still holds $0.90

    attempt(store, r2, item=1, price=PRICE)  # R2's retry: it needs fresh budget and must not see X's dead reservation

    swept = store.get_request(x)
    assert (swept["outcome"], swept["error_category"]) == (usage.FAILED, "abandoned")
    assert store.get_request(r2)["outcome"] == usage.PENDING  # the live request was left alone
    summary = window_summary(store)
    assert summary["reserved_remaining"] == 0  # X's reservation is gone
    assert summary["estimated_consumed"] == 2 * PRICE  # R2's two attempts, each counted once
    assert [a["attempt_number"] for a in store.list_attempts(r2)] == [1, 2]


@pytest.mark.parametrize("mode", MODES)
def test_the_same_retry_through_the_real_retry_path_in_services(workdir, store, clock, settings, mode):
    configure(settings, mode, "1.5", "0.45", margin=0)
    begin_images(store, 2)  # X: reserves $0.90, then "crashes"
    clock.advance(800)

    class SlowFailureThenImage(ScriptedProvider):
        """The first call is slow (X becomes stale meanwhile) and fails with a retryable 503."""

        def generate(self, prompt):
            self.calls += 1
            if self.calls == 1:
                clock.advance(300)
                raise make_http_error(503)
            return img()

    provider = SlowFailureThenImage([])
    (result,) = services.generate_images(prompt_data(1), [1], provider=provider, store=store)
    assert result.ok and provider.calls == 2  # without the sweep the retry would have been refused
    rows = {r["outcome"]: r for r in store.list_requests()}
    assert rows[usage.SUCCEEDED]["items_succeeded"] == 1
    assert (rows[usage.FAILED]["error_category"]) == "abandoned"  # X, swept at the retry


# ------------------------------------------------------------------ active requests are not swept
@pytest.mark.parametrize("mode", MODES)
def test_requests_that_are_still_active_are_not_swept_by_a_retry(store, clock, settings, mode):
    configure(settings, mode, "5", "0.45", margin=0)
    x = begin_images(store, 2)
    clock.advance(800)
    attempt(store, x, item=1, price=PRICE)  # X just made progress, so it is alive
    r2 = begin_images(store, 1)
    first = attempt(store, r2, item=1, price=PRICE)
    store.finish_attempt(first, usage.ATTEMPT_FAILED, error_category="server_error")
    clock.advance(200)  # X and R2 have each been silent for 200 s

    attempt(store, r2, item=1, price=PRICE)  # R2's retry runs the sweep

    assert store.get_request(x)["outcome"] == usage.PENDING  # not swept
    assert store.get_request(r2)["outcome"] == usage.PENDING
    assert window_summary(store)["reserved_remaining"] == PRICE  # X's share for its second image is still held
    assert store.sweep_stale() == 0  # and an explicit sweep agrees: nothing is stale


@pytest.mark.parametrize("mode", MODES)
def test_the_stale_threshold_is_exact(store, clock, settings, mode):
    configure(settings, mode, "5", "0.45", margin=0)
    still_ok = begin_images(store, 1)
    too_late = begin_images(store, 1)
    clock.advance(900)  # exactly the threshold: not stale (stale means silent for MORE than 900 s)
    attempt(store, still_ok, item=1, price=PRICE)
    assert store.get_request(too_late)["outcome"] == usage.PENDING
    clock.advance(1)  # 901 s
    with pytest.raises(ValueError):  # now it is stale: swept, and the attempt is refused
        attempt(store, too_late, item=1, price=PRICE)
    assert store.get_request(too_late)["outcome"] == usage.FAILED
    assert store.get_request(still_ok)["outcome"] == usage.PENDING  # its attempt a second ago keeps it alive


# ------------------------------------------------------------------ genuinely stale requests are still rejected
@pytest.mark.parametrize("mode", MODES)
def test_a_genuinely_stale_request_is_rejected_even_if_nobody_else_swept_it(store, clock, settings, mode):
    configure(settings, mode, "5", "0.45", margin=0)
    rid = begin_images(store, 1)
    clock.advance(1000)  # silent past the threshold, and no other request arrives to sweep it
    assert store.get_request(rid)["outcome"] == usage.PENDING

    provider = ScriptedProvider([img()])
    observer = usage.AttemptObserver(store, rid, HF, "m", item_ref=1, estimated_cost_micros=PRICE,
                                     quotas=limits.image_plan(HF, "m", 1).attempt_quotas)
    image, error = generateImage.generate_image_with_error("a cat", provider, observer)

    assert image is None and provider.calls == 0  # the provider was never contacted
    assert observer.last_error_category == "request_closed"
    assert store.list_attempts(rid) == []  # and nothing was recorded as spent
    row = store.get_request(rid)
    assert (row["outcome"], row["error_category"]) == (usage.FAILED, "abandoned")


@pytest.mark.parametrize("mode", MODES)
def test_a_swept_request_cannot_be_revived_by_a_later_attempt(store, clock, settings, mode):
    configure(settings, mode, "5", "0.45", margin=0)
    rid = begin_images(store, 1)
    clock.advance(1000)
    store.sweep_stale()
    for _ in range(2):  # trying again changes nothing
        with pytest.raises(ValueError):
            attempt(store, rid, item=1, price=PRICE)
    assert store.list_attempts(rid) == [] and store.get_request(rid)["outcome"] == usage.FAILED


# ------------------------------------------------------------------ no overspending from releasing a reservation
@pytest.mark.parametrize("mode", MODES)
def test_the_sweep_at_retry_time_keeps_the_cost_of_interrupted_calls(store, clock, settings, mode):
    configure(settings, mode, "1.5", "0.45", margin=0)
    x = begin_images(store, 2)
    attempt(store, x, item=1, price=PRICE)  # X died inside this call: it stays 'started'
    clock.advance(800)
    r2 = begin_images(store, 1)
    first = attempt(store, r2, item=1, price=PRICE)
    store.finish_attempt(first, usage.ATTEMPT_FAILED, error_category="server_error")
    clock.advance(200)

    attempt(store, r2, item=1, price=PRICE)  # the retry sweeps X

    assert store.get_request(x)["error_category"] == "abandoned"
    assert [a["status"] for a in store.list_attempts(x)] == ["started"]  # kept, not deleted or marked failed
    summary = window_summary(store)
    assert summary["estimated_consumed"] == 3 * PRICE  # X's interrupted call + R2's two attempts
    assert summary["reserved_remaining"] == 0  # only the unused share of X's second image was released


@pytest.mark.parametrize("mode", MODES)
def test_the_budget_is_still_enforced_after_the_sweep(store, clock, settings, mode):
    configure(settings, mode, "1.5", "0.45", margin=0)
    x, r2 = crashed_x_then_live_r2_with_a_failed_first_attempt(store, clock)
    clock.advance(200)
    attempt(store, r2, item=1, price=PRICE)  # fits once X's reservation is gone: $0.90 of $1.50
    attempt(store, r2, item=1, price=PRICE)  # a further retry: $1.35 of $1.50
    with pytest.raises(LimitExceededError) as exc:
        attempt(store, r2, item=1, price=PRICE)  # $1.80 would pass the ceiling
    assert exc.value.code == "budget_exceeded"
    assert window_summary(store)["committed"] == 3 * PRICE


# ------------------------------------------------------------------ concurrency
@pytest.mark.parametrize("mode", MODES)
def test_concurrent_retries_with_a_stale_request_sweep_it_once_and_respect_the_budget(tmp_path, clock, settings, mode):
    configure(settings, mode, "4.5", "0.45", margin=0)
    path = tmp_path / "race.db"
    first = SqliteUsageStore(path, clock=clock, stale_after_seconds=900)
    x = begin_images(first, 2)  # X crashes holding $0.90
    clock.advance(800)
    live = []
    for _ in range(6):  # six live requests, each with a failed first attempt ($0.45 each = $2.70)
        rid = begin_images(first, 1)
        a = attempt(first, rid, item=1, price=PRICE)
        first.finish_attempt(a, usage.ATTEMPT_FAILED, error_category="server_error")
        live.append(rid)
    clock.advance(200)  # X is stale; the six have been silent for only 200 s

    quotas = limits.image_plan(HF, "m", 1).attempt_quotas
    jobs = [lambda rid=rid: SqliteUsageStore(path, clock=clock, stale_after_seconds=900).start_attempt(
        rid, HF, "m", item_ref=1, estimated_cost_micros=PRICE, quotas=quotas) for rid in live]
    results, errors = run_threads(jobs)

    # with X's $0.90 released, $4.50 - $2.70 leaves room for exactly four $0.45 retries (it would be two if X still held it)
    assert len(results) == 4 and len(errors) == 2
    assert {e.code for e in errors} == {"budget_exceeded"}
    assert first.get_request(x)["error_category"] == "abandoned"
    assert [first.get_request(r)["outcome"] for r in live] == [usage.PENDING] * 6  # none of the live ones were swept
    assert window_summary(first)["committed"] == 4_500_000  # exactly the ceiling, never over
