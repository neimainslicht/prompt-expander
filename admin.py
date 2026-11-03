"""Administrator tool: review usage and budget, and correct the app's own accounting.

    python admin.py status
    python admin.py sweep-stale
    python admin.py adjust --provider huggingface --amount=-1.25 --note "dashboard shows lower spend"
    python admin.py record-actual --attempt-id 17 --usd 0.031

It works on the same database as the app (USAGE_DB_PATH). The app's figures are its own
estimates, not your provider's bill: compare them with the provider's dashboard before
changing anything. See README, 'Restoring generation'. Dollar amounts are exact and may
have at most 6 decimal places.
"""
import argparse
import sys

import config
import money
import usage
from usage import Quota

HF = "huggingface"


def _quota(metric, ceiling, **kwargs):
    return Quota(code="admin", message="", metric=metric, ceiling=ceiling, **kwargs)


def _usd(micros, places=4):
    return money.format_usd(micros, places)


def status_report(store, settings) -> str:
    tz = settings.quota_timezone
    cost = store.cost_summary(HF)
    ceiling = settings.hf_usable_budget_micros
    images_today = store.measure(_quota(usage.IMAGES, settings.max_images_per_day, day_timezone=tz))
    requests_minute = store.measure(_quota(usage.REQUESTS, settings.max_generation_requests_per_minute, window_seconds=60))
    gemini_minute = store.measure(_quota(usage.ATTEMPTS, settings.gemini_max_requests_per_minute,
                                         provider_prefix="gemini", window_seconds=60))
    gemini_day = store.measure(_quota(usage.ATTEMPTS, settings.gemini_max_requests_per_day,
                                      provider_prefix="gemini", day_timezone=tz))
    price = settings.hf_cost_per_image_micros
    lines = [
        f"Generation switch: {'ON' if settings.generation_enabled else 'OFF'} (GENERATION_ENABLED)",
        "",
        "Hugging Face budget (cumulative, all months; the app's estimate, not your provider balance)",
        f"  usable ceiling       {_usd(ceiling)}  (HF_BUDGET_USD {_usd(settings.hf_budget_micros, 2)}"
        f" minus {format(settings.hf_budget_safety_margin_percent.normalize(), 'f')}% margin)",
        f"  committed            {_usd(cost['committed'])}",
        f"    reserved (active)  {_usd(cost['reserved_remaining'])}",
        f"    estimated spent    {_usd(cost['estimated_consumed'])}   (every attempt, failed ones included)",
        f"    confirmed actual   {_usd(cost['confirmed_actual'])}",
        f"    adjustments        {_usd(cost['adjustments'])}",
        f"  remaining            {_usd(ceiling - cost['committed'])}",
        "  cost per image       "
        + (f"{_usd(price)} (HF_COST_PER_IMAGE_USD)" if price is not None
           else "NOT SET - image generation is refused until HF_COST_PER_IMAGE_USD is set"),
        "",
        f"Images today ({tz}):         {images_today} of {settings.max_images_per_day}",
        f"Generation requests, last minute: {requests_minute} of {settings.max_generation_requests_per_minute}",
        f"Gemini calls, last minute:   {gemini_minute} of {settings.gemini_max_requests_per_minute}",
        f"Gemini calls today ({tz}):   {gemini_day} of {settings.gemini_max_requests_per_day}",
        "",
        "Recent requests (newest first):",
    ]
    recent = store.list_requests(10)
    for r in recent:
        cost_text = "" if r["estimated_cost_micros"] is None else f" est {_usd(r['estimated_cost_micros'])}"
        lines.append(f"  {r['created_at']}  {r['operation']:17} {r['outcome']:19} "
                     f"{r['items_succeeded']}/{r['items_requested']}  {r['error_category'] or '-'}{cost_text}")
    if not recent:
        lines.append("  (none)")
    return "\n".join(lines)


def main(argv=None, store=None, settings=None) -> int:
    parser = argparse.ArgumentParser(prog="admin.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show budget, quotas and recent requests")
    sub.add_parser("sweep-stale", help="close requests abandoned by a crash and release their reservations")
    adj = sub.add_parser("adjust", help="record a manual correction to a provider's estimated spend")
    adj.add_argument("--provider", default=HF)
    adj.add_argument("--amount", required=True, help="USD; negative lowers the recorded spend (use --amount=-1.25)")
    adj.add_argument("--note", required=True, help="why (kept for the audit trail)")
    act = sub.add_parser("record-actual", help="record the actual billed cost of one attempt")
    act.add_argument("--attempt-id", type=int, required=True)
    act.add_argument("--usd", required=True, help="USD, at most 6 decimal places")
    args = parser.parse_args(argv)

    try:
        settings = settings or config.get_settings()
        store = store or usage.get_store()
        if args.command == "status":
            print(status_report(store, settings))
        elif args.command == "sweep-stale":
            print(f"Closed {store.sweep_stale()} abandoned request(s).")
        elif args.command == "adjust":
            amount = money.usd_to_micros(args.amount)
            adjustment_id = store.add_cost_adjustment(args.provider, amount, args.note)
            print(f"Recorded adjustment #{adjustment_id}: {args.provider} {_usd(amount, 6)}.")
        elif args.command == "record-actual":
            cost = money.usd_to_micros(args.usd)
            if not store.record_actual_cost(args.attempt_id, cost):
                print(f"No attempt with id {args.attempt_id}.")
                return 1
            print(f"Recorded actual cost {_usd(cost, 6)} for attempt {args.attempt_id}.")
    except (config.ConfigError, ValueError) as e:
        print(f"[Error] {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
