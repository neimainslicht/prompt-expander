"""Budget modes: cumulative (all history) vs monthly (current calendar month in Asia/Jerusalem).

Providers are fakes, time is a fake clock, and prices are made-up test prices. Nothing here calls a
real API or spends anything. Money below is in whole microdollars (1 USD = 1,000,000).
"""
from datetime import datetime, timedelta, timezone

import pytest

import admin
import config
import generatePrompts
import limits
import services
import usage
from conftest import ScriptedProvider
from errors import LimitExceededError
from test_limits import HF, Clock, attempt, begin_images, img, run_threads
from test_usage import FakeGemini, prompt_data, reply
from usage import SqliteUsageStore

UTC = timezone.utc

# first instants of a Jerusalem calendar month, in UTC (the offset changes with daylight saving)
BOUNDARIES = [
    pytest.param(datetime(2026, 10, 31, 22, 0, tzinfo=UTC), id="2026-11-01 00:00 (UTC+2)"),
    pytest.param(datetime(2026, 3, 31, 21, 0, tzinfo=UTC), id="2026-04-01 00:00 (daylight saving, UTC+3)"),
    pytest.param(datetime(2026, 12, 31, 22, 0, tzinfo=UTC), id="2027-01-01 00:00 (year rollover)"),
]


@pytest.fixture
def clock():
    return Clock(datetime(2026, 10, 15, 12, 0, tzinfo=UTC))


@pytest.fixture
def store(tmp_path, clock):
    return SqliteUsageStore(tmp_path / "modes.db", clock=clock, stale_after_seconds=900)


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def configure(settings, mode, budget, price, margin=10):
    """Set the budget configuration the way a .env file would, with the other limits out of the way."""
    settings(HF_BUDGET_MODE=mode, HF_BUDGET_USD=budget, HF_BUDGET_SAFETY_MARGIN_PERCENT=margin,
             HF_COST_PER_IMAGE_USD=price, MAX_IMAGES_PER_DAY=500, MAX_GENERATION_REQUESTS_PER_MINUTE=500)


def spend(store, count, price):
    """Make `count` finished one-image requests, each with one successful attempt costing `price`."""
    for _ in range(count):
        rid = begin_images(store, 1)
        aid = attempt(store, rid, item=1, price=price)
        store.finish_attempt(aid, usage.ATTEMPT_SUCCEEDED)
        store.finish_request(rid, usage.SUCCEEDED, 1)


def counted(store):
    """What the budget check currently counts, using exactly the window the real check uses."""
    return store.cost_summary("huggingface", limits.budget_quota(config.get_settings(), "huggingface"))["committed"]


def everything(store):
    """All stored rows, to prove nothing was deleted or altered."""
    return [(r, store.list_attempts(r["request_id"])) for r in store.list_requests(10_000)]


# ------------------------------------------------------------------ configuration
def test_default_is_cumulative_4_75_with_a_4_275_usable_ceiling():
    s = config.load_settings({})
    assert s.hf_budget_mode == "cumulative" and not s.hf_budget_is_monthly
    assert str(s.hf_budget_usd) == "4.75" and s.hf_usable_budget_micros == 4_275_000
    assert str(s.hf_usable_budget_usd) == "4.275"


def test_monthly_five_dollars_gives_a_4_50_usable_ceiling():
    s = config.load_settings({"HF_BUDGET_MODE": "monthly", "HF_BUDGET_USD": "5"})
    assert s.hf_budget_mode == "monthly" and s.hf_budget_is_monthly
    assert s.hf_usable_budget_micros == 4_500_000 and str(s.hf_usable_budget_usd) == "4.5"


def test_the_margin_is_applied_once_and_the_check_uses_that_exact_ceiling():
    for env, ceiling in (({}, 4_275_000), ({"HF_BUDGET_MODE": "monthly", "HF_BUDGET_USD": "5"}, 4_500_000)):
        s = config.load_settings(env)
        assert s.hf_usable_budget_micros == ceiling  # budget x 90%, once
        quota = limits.budget_quota(s, "huggingface")
        assert quota.ceiling == ceiling  # not reduced a second time on the way into the check
        assert limits.image_plan("huggingface/test", "m", 1, settings=config.load_settings(
            {**env, "HF_COST_PER_IMAGE_USD": "0.05"})).attempt_quotas[0].ceiling == ceiling


def test_mode_is_case_and_space_insensitive_but_otherwise_strict():
    assert config.load_settings({"HF_BUDGET_MODE": " Cumulative "}).hf_budget_mode == "cumulative"
    assert config.load_settings({"HF_BUDGET_MODE": "MONTHLY", "HF_BUDGET_USD": "5"}).hf_budget_mode == "monthly"
    for bad in ("weekly", "daily", "", "   ", "none", "month"):
        with pytest.raises(config.ConfigError, match="HF_BUDGET_MODE"):
            config.load_settings({"HF_BUDGET_MODE": bad})


def test_unset_mode_means_cumulative_the_stricter_choice():
    assert config.load_settings({"HF_BUDGET_USD": "4.75"}).hf_budget_mode == "cumulative"


def test_monthly_mode_never_inherits_the_remaining_balance_default():
    with pytest.raises(config.ConfigError, match="HF_BUDGET_MODE=monthly requires HF_BUDGET_USD"):
        config.load_settings({"HF_BUDGET_MODE": "monthly"})
    config.load_settings({"HF_BUDGET_MODE": "cumulative"})  # cumulative may use the built-in default


@pytest.mark.parametrize("mode", ["cumulative", "monthly"])
@pytest.mark.parametrize("bad", ["0", "-5", "abc", "", "4.7500001", "nan", "inf"])
def test_invalid_budget_amounts_are_refused_in_both_modes(mode, bad):
    with pytest.raises(config.ConfigError, match="HF_BUDGET_USD"):
        config.load_settings({"HF_BUDGET_MODE": mode, "HF_BUDGET_USD": bad})


def test_an_invalid_budget_configuration_stops_generation_before_any_provider_call(workdir, store, settings,
                                                                                    monkeypatch):
    settings(HF_BUDGET_MODE="weekly")
    provider = ScriptedProvider([img()])
    with pytest.raises(config.ConfigError, match="HF_BUDGET_MODE"):
        services.generate_images(prompt_data(1), [1], provider=provider, store=store)
    client = FakeGemini([reply()])
    monkeypatch.setattr(generatePrompts, "get_client", lambda: client)
    result = services.expand_idea("a cat", store=store)
    assert not result.ok and result.error_code == "config_error" and "HF_BUDGET_MODE" in result.error
    assert provider.calls == 0 and client.calls == 0 and store.list_requests() == []


# ------------------------------------------------------------------ cumulative mode
def test_cumulative_ceiling_fits_exactly_ten_images_then_refuses(store, settings):
    configure(settings, "cumulative", "4.75", "0.4275")  # 10 x 0.4275 = 4.275 = the usable ceiling
    spend(store, 10, 427_500)
    assert counted(store) == 4_275_000
    with pytest.raises(LimitExceededError) as exc:
        begin_images(store, 1)
    assert exc.value.code == "budget_exceeded" and "$4.2750" in exc.value.message
    assert "cumulative" in exc.value.message and exc.value.retry_after_seconds is None  # nothing will free it


def test_cumulative_mode_never_resets_across_months_or_restarts(tmp_path, clock, settings):
    configure(settings, "cumulative", "4.75", "0.4275")
    first = SqliteUsageStore(tmp_path / "persist.db", clock=clock)
    spend(first, 10, 427_500)
    for days in (1, 20, 45, 120, 400):  # well across month and year boundaries
        clock.advance(days * 24 * 3600)
        restarted = SqliteUsageStore(tmp_path / "persist.db", clock=clock)  # a fresh process on the same file
        assert counted(restarted) == 4_275_000
        with pytest.raises(LimitExceededError):
            begin_images(restarted, 1)


# ------------------------------------------------------------------ monthly mode
def test_monthly_ceiling_fits_exactly_ten_images_then_refuses_until_the_1st(store, settings):
    configure(settings, "monthly", "5", "0.45")  # 10 x 0.45 = 4.50 = the usable ceiling
    spend(store, 10, 450_000)
    assert counted(store) == 4_500_000
    with pytest.raises(LimitExceededError) as exc:
        begin_images(store, 1)
    assert exc.value.code == "budget_exceeded" and "$4.5000" in exc.value.message
    assert "monthly" in exc.value.message and "1st of next month (Asia/Jerusalem)" in exc.value.message
    assert 0 < exc.value.retry_after_seconds <= 31 * 24 * 3600  # seconds until the budget starts again


@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_monthly_budget_resets_exactly_at_jerusalem_midnight(store, clock, settings, boundary):
    configure(settings, "monthly", "5", "0.45")
    clock.now = boundary - timedelta(hours=1)
    spend(store, 10, 450_000)
    clock.now = boundary - timedelta(seconds=1)  # 23:59:59 Jerusalem time on the last day
    with pytest.raises(LimitExceededError) as exc:
        begin_images(store, 1)
    assert exc.value.retry_after_seconds == 1
    clock.now = boundary  # 00:00:00 on the 1st
    begin_images(store, 1)
    assert counted(store) == 450_000  # only the new month's reservation


def test_the_boundary_follows_jerusalem_time_not_utc(store, clock, settings):
    configure(settings, "monthly", "5", "0.45")
    # a UTC date change in the middle of a Jerusalem month does nothing to the budget
    clock.now = datetime(2026, 10, 30, 23, 0, tzinfo=UTC)  # 01:00 on Oct 31 in Jerusalem
    spend(store, 10, 450_000)
    clock.now = datetime(2026, 10, 31, 0, 0, tzinfo=UTC)  # UTC midnight: not a Jerusalem month boundary
    with pytest.raises(LimitExceededError):
        begin_images(store, 1)
    # the Jerusalem month ends 2 hours before the UTC one does: the budget is already new at 00:30 Jerusalem time
    clock.now = datetime(2026, 10, 31, 22, 30, tzinfo=UTC)  # 00:30 on Nov 1 in Jerusalem, still Oct 31 in UTC
    begin_images(store, 1)


def test_history_survives_the_month_boundary_and_stays_visible(store, clock, settings):
    configure(settings, "monthly", "5", "0.45")
    clock.now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    spend(store, 10, 450_000)
    before = everything(store)
    clock.now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    spend(store, 3, 450_000)
    assert counted(store) == 1_350_000  # this month only
    assert store.cost_summary("huggingface")["committed"] == 13 * 450_000  # all history, kept
    assert everything(store)[3:] == before  # the October rows are untouched (newest first, so they follow)


def test_a_request_pending_across_the_boundary_counts_where_its_attempts_happen(store, clock, settings):
    configure(settings, "monthly", "5", "0.45")
    clock.now = datetime(2026, 10, 31, 21, 59, 0, tzinfo=UTC)  # a minute before November in Jerusalem
    rid = begin_images(store, 2)  # reserves one attempt for each of two images
    attempt(store, rid, item=1, price=450_000)  # image 1 starts in October
    clock.now = datetime(2026, 10, 31, 22, 1, 0, tzinfo=UTC)  # November now (the request is still live: 2 minutes)
    summary = store.cost_summary("huggingface", limits.budget_quota(config.get_settings(), "huggingface"))
    assert summary["estimated_consumed"] == 0  # image 1's attempt belongs to October
    assert summary["reserved_remaining"] == 450_000  # image 2 is still to come, and will run in November
    attempt(store, rid, item=2, price=450_000)
    summary = store.cost_summary("huggingface", limits.budget_quota(config.get_settings(), "huggingface"))
    assert (summary["estimated_consumed"], summary["reserved_remaining"]) == (450_000, 0)  # counted once


def test_adjustments_and_confirmed_costs_belong_to_the_month_they_were_entered_or_started(store, clock, settings):
    configure(settings, "monthly", "5", "0.45")
    clock.now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    rid = begin_images(store, 1)
    aid = attempt(store, rid, price=450_000)
    store.finish_attempt(aid, usage.ATTEMPT_SUCCEEDED)
    store.finish_request(rid, usage.SUCCEEDED, 1)
    store.add_cost_adjustment("huggingface", 1_000_000, "october correction")
    clock.now = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    assert store.record_actual_cost(aid, 400_000)  # a late confirmation of an October attempt
    assert counted(store) == 0  # all of that is October's
    store.add_cost_adjustment("huggingface", 250_000, "november correction")
    assert counted(store) == 250_000
    config_cumulative = config.load_settings({})
    assert store.cost_summary("huggingface", limits.budget_quota(config_cumulative, "huggingface"))["committed"] \
        == 400_000 + 1_000_000 + 250_000  # cumulative counts every adjustment ever recorded


# ------------------------------------------------------------------ switching modes
def test_switching_from_cumulative_to_monthly_and_back_deletes_nothing(store, clock, settings):
    """The exact example from DEVELOPMENT_GUIDE.md section 6."""
    clock.now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    configure(settings, "cumulative", "4.75", "0.4275")
    spend(store, 6, 427_500)  # October, cumulative mode: 6 images = $2.565
    october = everything(store)

    # edit .env: HF_BUDGET_MODE=monthly, HF_BUDGET_USD=5, restart (a new month has started)
    clock.now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    configure(settings, "monthly", "5", "0.4275")
    assert config.get_settings().hf_usable_budget_micros == 4_500_000
    assert counted(store) == 0  # October's $2.565 is not counted in November...
    assert everything(store) == october  # ...but it is all still there
    spend(store, 10, 427_500)  # 10 x 0.4275 = 4.275, within November's $4.50
    with pytest.raises(LimitExceededError):  # an 11th would be $4.7025 > $4.50
        begin_images(store, 1)
    assert everything(store)[10:] == october  # the original rows are unchanged, newest-first order

    # edit .env back to cumulative and restart: now ALL history counts again
    configure(settings, "cumulative", "4.75", "0.4275")
    assert counted(store) == 16 * 427_500  # 6 + 10 images = $6.84, over the $4.275 ceiling
    with pytest.raises(LimitExceededError):
        begin_images(store, 1)
    assert len(everything(store)) == 16


# ------------------------------------------------------------------ admin report
@pytest.mark.parametrize("mode,budget,ceiling,mode_text,history_text", [
    ("cumulative", "4.75", "$4.2750", "CUMULATIVE mode", "(all of it is counted)"),
    ("monthly", "5", "$4.5000", "MONTHLY mode", "(kept; only this month is counted)"),
])
def test_status_report_names_the_mode_and_keeps_history_visible(store, clock, settings, capsys,
                                                               mode, budget, ceiling, mode_text, history_text):
    clock.now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    configure(settings, "cumulative", "4.75", "0.05")
    spend(store, 2, 50_000)  # $0.10 in October
    clock.now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    configure(settings, mode, budget, "0.05")
    assert admin.main(["status"], store=store) == 0
    out = capsys.readouterr().out
    assert mode_text in out and f"usable ceiling       {ceiling}" in out
    assert "all recorded history $0.1000" in out and history_text in out
    committed_line = next(line for line in out.splitlines() if line.strip().startswith("committed"))
    assert committed_line.split()[-1] == ("$0.1000" if mode == "cumulative" else "$0.0000")


def test_admin_refuses_cleanly_on_an_invalid_budget_configuration(settings, store, capsys, monkeypatch):
    settings(HF_BUDGET_MODE="monthly")  # monthly without HF_BUDGET_USD is not allowed...
    monkeypatch.delenv("HF_BUDGET_USD")  # ...and the shared test defaults had put a value there
    config.get_settings.cache_clear()
    assert admin.main(["status"], store=store) == 1
    assert "HF_BUDGET_USD" in capsys.readouterr().out


# ------------------------------------------------------------------ concurrency and retries, both modes
MODES = [
    pytest.param("cumulative", "4.75", "0.4275", id="cumulative $4.75 -> $4.275"),
    pytest.param("monthly", "5", "0.45", id="monthly $5 -> $4.50"),
]


@pytest.mark.parametrize("mode,budget,price", MODES)
def test_concurrent_requests_cannot_reserve_the_same_budget(tmp_path, clock, settings, mode, budget, price):
    configure(settings, mode, budget, price)
    path = tmp_path / "race.db"
    SqliteUsageStore(path, clock=clock)
    results, errors = run_threads([lambda: begin_images(SqliteUsageStore(path, clock=clock), 1) for _ in range(24)])
    assert len(results) == 10 and len(errors) == 14  # ten reservations fill the usable ceiling exactly
    assert {e.code for e in errors} == {"budget_exceeded"}
    assert counted(SqliteUsageStore(path, clock=clock)) == config.get_settings().hf_usable_budget_micros


@pytest.mark.parametrize("mode", ["cumulative", "monthly"])
def test_retry_accounting_is_identical_in_both_modes(store, settings, mode):
    configure(settings, mode, "1.2", "0.3", margin=0)  # usable ceiling $1.20 in either mode
    price = 300_000
    rid = begin_images(store, 2)  # reserves one attempt per image: $0.60
    assert counted(store) == 600_000
    attempt(store, rid, item=1, price=price)  # image 1, first attempt: uses image 1's own share
    assert counted(store) == 600_000
    attempt(store, rid, item=1, price=price)  # retry: fresh budget
    attempt(store, rid, item=1, price=price)  # second retry: now at the ceiling
    assert counted(store) == 1_200_000
    with pytest.raises(LimitExceededError):  # a third retry would pass the ceiling
        attempt(store, rid, item=1, price=price)
    attempt(store, rid, item=2, price=price)  # image 2's reserved first attempt was never touched
    summary = store.cost_summary("huggingface", limits.budget_quota(config.get_settings(), "huggingface"))
    assert summary["estimated_consumed"] == 4 * price  # four started attempts, none of them finished: all charged
    assert summary["reserved_remaining"] == 0 and summary["committed"] == 1_200_000


def test_a_retry_after_the_boundary_is_charged_to_the_new_month_but_both_attempts_are_kept(store, clock, settings):
    configure(settings, "monthly", "5", "0.45")
    clock.now = datetime(2026, 10, 31, 21, 59, 0, tzinfo=UTC)
    rid = begin_images(store, 1)
    first = attempt(store, rid, item=1, price=450_000)  # October
    store.finish_attempt(first, usage.ATTEMPT_FAILED, error_category="server_error")
    clock.now = datetime(2026, 10, 31, 22, 0, 30, tzinfo=UTC)  # 30 seconds into November
    attempt(store, rid, item=1, price=450_000)  # the retry
    window = limits.budget_quota(config.get_settings(), "huggingface")
    assert store.cost_summary("huggingface", window)["estimated_consumed"] == 450_000  # November: the retry only
    assert store.cost_summary("huggingface")["estimated_consumed"] == 900_000  # history: both, each counted once
    assert [a["attempt_number"] for a in store.list_attempts(rid)] == [1, 2]  # still one request, two attempts


# ------------------------------------------------------------------ through the service layer
def test_services_refuse_in_monthly_mode_and_recover_on_the_1st(workdir, store, clock, settings):
    configure(settings, "monthly", "1", "0.5", margin=0)  # usable $1.00 per month, $0.50 per image
    clock.now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    first = services.generate_images(prompt_data(2), [1, 2], provider=ScriptedProvider([img()]), store=store)
    assert all(r.ok for r in first)  # $1.00 spent
    provider = ScriptedProvider([img()])
    with pytest.raises(LimitExceededError) as exc:
        services.generate_images(prompt_data(1), [1], provider=provider, store=store)
    assert exc.value.code == "budget_exceeded" and "monthly budget" in exc.value.message
    assert provider.calls == 0 and exc.value.retry_after_seconds > 0
    clock.now = datetime(2026, 10, 31, 22, 0, 1, tzinfo=UTC)  # just after midnight on the 1st (Jerusalem)
    again = services.generate_images(prompt_data(1), [1], provider=provider, store=store)
    assert again[0].ok and provider.calls == 1
    assert len(store.list_requests()) == 3  # the refusal was recorded too; nothing was removed
