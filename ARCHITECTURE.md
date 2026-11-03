# Prompt Expander: Architecture

State described here: git `main` at commit `6215628`, **plus the budget-modes change described in section 9 ("Budget modes"), which was made afterwards, on top of that commit**. Written 2026-11-03. Everything below was checked against the code unless it is explicitly marked *(not verified)*.

Related documents: [DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md) (how to work on it), [CHECKPOINT_STATUS.md](CHECKPOINT_STATUS.md) (what is and isn't done), [HANDOFF.md](HANDOFF.md) (start here when resuming).

---

## 1. What the application does

Prompt Expander turns a short image idea into several detailed text-to-image prompts, and can then turn chosen prompts into images.

1. **Expand:** the user types an idea ("ad for shakespeare in the park"). Gemini (`gemini-2.5-flash`) returns up to 10 detailed prompts. They are saved as a JSON file in `output/`.
2. **Generate:** the user picks up to 2 saved prompts. Hugging Face Inference Providers (route `fal-ai`, model `black-forest-labs/FLUX.1-dev`) renders each as a PNG in `output/images/`, and the prompt's status in the JSON is updated.

Everything else in the codebase exists to make those two calls **safe**: bounded in size, switchable off, recorded, rate-limited, quota-limited and budget-limited, so that a future public website cannot run up unexpected provider costs.

Currently there is only a terminal UI (Questionary). A React + FastAPI front end is the long-term goal and is **not started**.

---

## 2. The big picture

```
 You
  │  keyboard
  ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ main.py            terminal UI only: questionary menus, input(), print()     │
└──────────────┬───────────────────────────────────────────────────────────────┘
               │ calls (never talks to providers or the database itself)
               ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ services.py        the workflows: expand_idea(s), generate_images,           │
│                    list_prompt_files, load_prompt_file                       │
│                    returns ExpandResult / ImageResult, raises AppError       │
└──┬───────────┬──────────────┬─────────────────────┬──────────────────────────┘
   │           │              │                     │
   ▼           ▼              ▼                     ▼
validation.py  limits.py      usage.py              fileUtils.py
(size limits,  (builds the    (UsageStore ABC +     (prompt JSON and PNG
 on/off switch) quotas)       SqliteUsageStore,      files on disk)
               pricing.py     AttemptObserver)
               (the only
                price source)
                                   │ one observer per request/item is handed down
                                   ▼
┌───────────────────────────────┐   ┌───────────────────────────────────────────┐
│ generatePrompts.py  (Gemini)  │   │ generateImage.py  (ImageProvider ABC +    │
│ call_gemini + tenacity retry  │   │ HuggingFaceImageProvider + tenacity retry)│
└───────────────┬───────────────┘   └───────────────────┬───────────────────────┘
                ▼                                       ▼
        Google Gemini API                  Hugging Face Inference Providers (fal-ai)

 Shared by everyone:  config.py (all settings, secrets)   errors.py (AppError family)
                      money.py (exact money)              logging_config.py (redaction)
 Separate entry point: admin.py (review/correct usage and budget)
```

**The key rule:** the user interface (`main.py`, and later FastAPI) calls only `services.py`. The provider modules refuse to call a provider unless `services.py` has first authorised the request. That is what makes the safeguards impossible to skip by accident (see §8).

---

## 3. Files and responsibilities

| File | Lines | Responsibility | Depends on |
|---|---:|---|---|
| `main.py` | 168 | CLI only. Menus, prompts, printing. Catches `AppError` and shows `e.message`. Runs `config.get_settings()` at startup and exits with code 1 on invalid configuration. | `services`, `config`, `errors`, `logging_config` |
| `services.py` | 329 | The workflows. Validates, authorises (limits + reservation), calls providers through observers, saves files, records outcomes. No `print`, no `input`, no API keys. | everything below |
| `validation.py` | 82 | Size/shape checks and the generation on/off switch. Raises `ValidationError` / `GenerationDisabledError`. Never truncates input. | `config`, `errors` |
| `limits.py` | 116 | Turns settings into a `Plan` (which `Quota`s to check before a request, which before each provider call, what to reserve). | `config`, `pricing`, `usage`, `money` |
| `pricing.py` | 36 | The only place a provider call's cost is decided. Gemini = 0 (free tier); Hugging Face = `HF_COST_PER_IMAGE_USD`; unknown provider or unset price raises `PricingUnavailableError`. | `errors` |
| `usage.py` | 780 | Usage database: `UsageStore` interface, `SqliteUsageStore`, `Quota`, `AttemptObserver`, atomic limit checks, reservations, stale-request sweep, cost summaries, schema upgrade. | `config`, `money`, `errors` |
| `generatePrompts.py` | 128 | Gemini integration: lazy client with 60 s timeout, `call_gemini` with retry, `generate_prompt_text`. | `config`, `usage`, `validation` |
| `generateImage.py` | 179 | `ImageProvider` interface, `HuggingFaceImageProvider`, retry (`call_image_api`), `generate_image_with_error`. | `config`, `usage`, `validation` |
| `fileUtils.py` | 86 | Prompt JSON (parse/save/update status/list/load) and PNG saving. Paths are **relative to the current directory** (`output/`). | stdlib only |
| `config.py` | 292 | All settings, validated; credentials via `require_secret`; timeout constants. | `money` |
| `errors.py` | 84 | `AppError` and its subclasses (stable `code`, `message`, `http_status`). | none |
| `money.py` | 47 | Exact money: `Decimal` at the edges, whole microdollars inside. | none |
| `logging_config.py` | 57 | `redact()` (secret scrubbing) and `configure_logging()`. | none |
| `admin.py` | 128 | Administrator CLI: `status`, `adjust`, `record-actual`, `sweep-stale`. | `config`, `usage`, `money` |
| `.env.example` | | Placeholder settings; copy to `.env`. | |
| `requirements.txt` | | Direct runtime + test dependencies. | |

Tests live in `tests/` (345 tests; see [DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md)).

`README.md` exists and covers configuration, limits and budget in detail, but its short **Architecture** section near the top is **out of date** (it lists only four modules). This document replaces it as the authoritative description.

---

## 4. Execution flow

### 4.1 Single idea ("Single Idea" menu item)

`main.process_single_idea()` → `input()` → `services.expand_idea(idea)`:

1. **Open the usage database:** `usage.get_store()` (a `SqliteUsageStore` at `USAGE_DB_PATH`). If that fails → failed `ExpandResult` with code `usage_unavailable`. *Nothing is called.*
2. **Validate** (`validation.py`): generation switch on? idea non-empty and ≤ 500 characters (measured without surrounding whitespace)? prompt count ≤ 10? A refusal is **recorded as a `rejected` request** (for diagnostics only; it counts as no usage) and returned as a failed `ExpandResult` with an `error_code`.
3. **Plan** (`limits.prompt_plan`): builds three quotas: requests per minute, Gemini calls per minute, Gemini calls per day.
4. **Authorise** (`store.begin_request(..., quotas=...)`): in **one** `BEGIN IMMEDIATE` transaction it first closes stale crashed requests, then checks every quota, then inserts a `pending` request. If a quota would be exceeded it raises `LimitExceededError`; the request is recorded as `rejected` and returned as a failed result carrying `retry_after_seconds` where the limit will clear.
5. **Create the observer** (`usage.AttemptObserver`), the "permit" the provider code requires.
6. **Call Gemini** (`generatePrompts.generate_prompt_text`): re-validates, requires the observer, then `call_gemini` under a tenacity retry. **Every real call**, including each retry, does `observer.started()` (re-checks the per-provider quotas and inserts a `provider_attempts` row *before* the call) and `observer.finished()` (stores success/failure, HTTP status, token counts).
7. **Save:** `fileUtils.parseJSON` → trimmed to the requested number of prompts (Gemini may return more) → `saveJSON` writes `output/<concept>_<timestamp>.json` with every prompt `"status": "not generated"`.
8. **Close the record** (in a `finally`): `store.finish_request(...)` sets the outcome (`succeeded` / `partially_succeeded` / `failed`), the number of prompts produced, and a short error category.

### 4.2 Batch of ideas ("Batch Mode")

`main.process_batch_mode()` asks for a `.txt` path (default `input/batch.txt`), reads one idea per non-empty line, and calls `services.expand_ideas(ideas, on_progress, on_wait)`. Each idea is its own request through the flow above, so **one idea failing never stops the rest**. There is a 1.5 s pause between ideas. If an idea is refused with code `rate_limited` (3 requests/minute) the batch waits for the window to clear (calling `on_wait` first, which the CLI turns into a visible `[Waiting] ...` line), up to 3 times per idea. *Gap:* only the code `rate_limited` is waited for; `provider_rate_limited` / `provider_daily_limit` (Gemini caps) are not, so those ideas just fail (see CHECKPOINT_STATUS.md).

### 4.3 Image generation ("Generate Images From Prompts")

`main.process_image_generation()`:

1. Early check `services.ensure_generation_enabled()`. If off, print the message and return.
2. List `output/*.json`; the user picks a file; `services.load_prompt_file` loads it (only names from the listing are accepted, so callers can't reach other paths).
3. A Questionary checkbox shows the prompts. Its `validate=` callback refuses a selection larger than `MAX_IMAGES_PER_REQUEST` with an explanatory message (nothing is silently dropped).
4. `services.generate_images(prompt_data, ids, on_progress)`:
   1. **Validate everything first** (raises `AppError`, records a `rejected` request): switch on, count ≤ 2, no duplicate prompt ids, every chosen prompt non-empty and ≤ 2000 characters. If any prompt is invalid, **nothing** is generated.
   2. **Resolve the provider** (`generateImage.get_provider`: `IMAGE_PROVIDER` env var, default `huggingface`). A failure here becomes per-image failures and a `failed` request with no attempts.
   3. **Price and authorise the whole batch before the first call** (`limits.image_plan` then `store.begin_request`): checks requests/minute, images today, and the Hugging Face budget, and **reserves** the daily images plus exactly **one attempt per image** of budget. If the price isn't configured, raises `PricingUnavailableError`. If anything would be exceeded, raises `LimitExceededError`. Either way, no provider call is made.
   4. **Loop over the prompts.** For each: build a per-image `AttemptObserver`, call `generate_image_with_error` (re-validates, requires the observer, then `call_image_api` with retries; each retry is another attempt that needs *additional* budget). On success the PNG is saved to `output/images/<concept>-prompt-<id>-<timestamp>.png` (never overwriting an earlier file) and the prompt's status becomes `generated`. On failure the status becomes `failed` (or stays `generated` if an earlier image exists). **One image failing never affects the others.**
   5. **`finally`:** the prompt JSON is saved (even on Ctrl+C), and the usage record is closed with the outcome and counts. Images never attempted release their reservation.

---

## 5. Gemini integration (`generatePrompts.py`)

- **Client:** created on first use (`get_client`) so importing the module needs no key. Key from `config.require_secret("API_KEY")`. Built with `http_options=HttpOptions(timeout=60_000)` (milliseconds): the SDK has no timeout by default, and an unbounded call would defeat stale-request detection.
- **Model:** `GEMINI_MODEL = "gemini-2.5-flash"` (module constant, also stored on each usage record).
- **Prompt:** an f-string asking for `{num_prompts}` prompts as JSON `{"prompts":[{"text":...}]}`; `response_mime_type="application/json"`. The user's idea is interpolated into the prompt text.
- **Retry:** `@retry` (tenacity) on `call_gemini`: up to 5 attempts, `wait_random_exponential(multiplier=0.5, min=1, max=60)`, retrying only `APIError` with code **503**, or **429** whose message does not contain "per day". The decorator re-runs the *whole function*, which is why attempt recording sits inside it.
- **Accounting:** `observer.started()` before `generate_content`, `observer.finished(...)` after, with `prompt_token_count` and `candidates_token_count` from `response.usage_metadata`. Gemini is treated as **free tier**: cost 0, limited only by request counts.
- **Result contract:** `generate_prompt_text` returns `(text, None)` or `(None, error_message)`; it never raises for expected failures. Messages are passed through `redact()`.

## 6. Hugging Face integration (`generateImage.py`)

- **Abstraction:** `ImageProvider` (ABC) requires `generate(prompt)` → PIL image and `is_retryable(exc)`; optional `retry_after(exc)`, `describe_error(exc)`, `usage_identity()` → `(provider_label, model)`. A registry `PROVIDERS = {"huggingface": HuggingFaceImageProvider}` plus `get_provider(name)` selects one (name argument, else `IMAGE_PROVIDER`, else `huggingface`).
- **`HuggingFaceImageProvider`:** defaults `provider="fal-ai"`, `model="black-forest-labs/FLUX.1-dev"`, `timeout=120` (from `config.HF_TIMEOUT_SECONDS`). The `InferenceClient` is created lazily with `config.require_secret("HF_TOKEN")`. `usage_identity()` returns `("huggingface/fal-ai", model)`; the part before `/` is the *provider family* used by pricing and budget queries.
- **Retry (`call_image_api`):** tenacity `Retrying`, up to **5 attempts**, `wait_random_exponential(multiplier=0.5, min=1, max=60)`, raising the *original* exception when exhausted (`reraise=True`). Retryable: HTTP 429/500/502/503/504, `InferenceTimeoutError`, `httpx.TransportError`. A `Retry-After` header is honoured (capped at 60 s). `describe_error` turns 401/402/403 into friendly messages ("Check HF_TOKEN", "Inference credits exhausted", ...).
- **Accounting:** inside `call_image_api`, a wrapper around `provider.generate` calls `observer.started()` / `observer.finished(...)` for **every attempt**, so a retried image produces several `provider_attempts` rows under one request.
- **Model and price:** the model is fixed in the provider's defaults (not an env var). The configured per-call price is the same for any model, so changing the model means revisiting `HF_COST_PER_IMAGE_USD`.

## 7. Retry logic and attempt accounting

Retries live in the provider modules (tenacity). Accounting is attached to them through the **observer**:

```
tenacity retries call_image_api / call_gemini
   └─ each attempt:  observer.started()   → usage.start_attempt(): re-check quotas, insert attempt row  (may raise → no call made)
                     provider call
                     observer.finished() → usage.finish_attempt(): status, error category, HTTP status, tokens
```

Consequences (all covered by tests):

- A retry is a new **attempt** under the **same request**. It never creates a second request.
- The attempt row is written *before* the call. If the process dies mid-call, the row stays `started` and is **charged as a possible expense**.
- An attempt blocked by a limit (provider cap, or no budget for a retry) raises before the call, so the provider is never contacted.
- `finished()` never raises: the call already happened, so a recording failure is only logged.
- Every attempt is charged `HF_COST_PER_IMAGE_USD`, **whether it succeeded, failed or was interrupted**. The app deliberately does not assume a failed call is free.

---

## 8. The service boundary ("permit") rule

`call_gemini`, `generate_prompt_text`, `call_image_api` and `generate_image_with_error` call `usage.require_observer(observer)` and refuse to run unless given a real `usage.AttemptObserver`. Only `services.py` creates one, and only *after* limits are checked and the request is recorded. Verified behaviour: calling these functions directly returns/raises "Generation must be started through the service layer" and the provider is never touched. A permit also stops working once its request is closed.

This stops *accidental* bypass. It does not defend against deliberately malicious code in the same process (an attacker could build an observer). For a public API, the real protection is that FastAPI exposes only `services.py`-level operations.

*Side effect:* the older convenience functions `generatePrompts.generatePrompt()` and `generateImage.generate_image()` (no observer argument in their original form) now always fail with that message. They are unused by the app (see CHECKPOINT_STATUS.md).

---

## 9. Usage tracking and the database

`usage.py`, SQLite file at `USAGE_DB_PATH` (default `usage.db`, relative to the current directory, git-ignored). `PRAGMA user_version = 3`, WAL journal mode, foreign keys on. Every operation opens a short-lived connection with a 10 s busy timeout; writes use `BEGIN IMMEDIATE` so they are serialised. Store creation is a no-op when the schema is already current.

### Tables

**`requests`**: one row per logical request (one idea expanded, or one batch of images).

| Column | Type | Meaning |
|---|---|---|
| `request_id` | TEXT PK | random UUID hex |
| `user_id` | TEXT | `local` until there is authentication |
| `created_at`, `finished_at` | TEXT | UTC ISO-8601 with milliseconds (sorts chronologically) |
| `operation` | TEXT | `prompt_generation` or `image_generation` |
| `provider`, `model` | TEXT | e.g. `gemini` / `huggingface/fal-ai` |
| `items_requested`, `items_succeeded` | INTEGER | prompts or images asked for / delivered |
| `outcome` | TEXT | `pending`, `succeeded`, `partially_succeeded`, `failed`, `rejected` |
| `estimated_cost_micros`, `actual_cost_micros` | INTEGER | totals from the attempts, set when the request closes |
| `error_category` | TEXT | short, safe label (never a raw message) |
| `reserved_images`, `reserved_cost_micros` | INTEGER | the reservation held while `pending` |
| `last_activity_at` | TEXT | updated when an attempt starts or ends; drives stale detection |

**`provider_attempts`**: one row per real provider call, retries included.

`attempt_id` (PK), `request_id` (FK), `item_ref` (the prompt id in a batch, `0` for prompt generation), `attempt_number` (1, 2, 3 for retries of the same item), `provider`, `model`, `started_at`, `finished_at`, `status` (`started` / `succeeded` / `failed`), `error_category`, `http_status`, `input_tokens`, `output_tokens`, `estimated_cost_micros`, `actual_cost_micros`. `UNIQUE (request_id, item_ref, attempt_number)`.

**`cost_adjustments`**: manual corrections entered by an administrator: `adjustment_id`, `provider`, `amount_micros` (may be negative), `note`, `created_at`.

Indexes: `idx_requests_created`, `idx_attempts_started`, `idx_attempts_provider`.

**Never stored:** API keys, prompt text, raw provider error messages. Prompts are referred to only by numeric id.

**Upgrades:** `SqliteUsageStore` upgrades older files in place (v1 → v3, converting the old floating-point cost columns to integer microdollars). *(The v1 and v2 paths are exercised by tests with hand-built files; they have not been run against a real older `usage.db`.)*

### How usage and money are counted (no double counting)

1. **Spending is derived from attempts.** Each attempt costs its `estimated_cost_micros`, or its `actual_cost_micros` if an administrator recorded one. Status doesn't matter: failed and `started` (interrupted) attempts cost too.
2. **A pending image request holds a reservation** of one first attempt per image. Each image's share is released the moment that image gets its first attempt (that attempt now holds the money). **Retries are never covered by a reservation**: each retry needs additional budget, checked just before it is made, so a retry can neither pass the budget nor use another image's share.
3. **Daily image quota counts images, not attempts**: an active request counts the images it reserved; a finished request counts the images that actually got an attempt. Retries are never extra images.
4. **Provider request limits count every attempt**, retries included.
5. **Rejected requests have no attempts** and are excluded from every count.
6. **The Hugging Face budget is counted per `HF_BUDGET_MODE`:** `cumulative` (the current setting) adds up all recorded spending and never resets; `monthly` adds up only the current calendar month (see *Budget modes* below). Either way it is compared against `Settings.hf_usable_budget_micros`, which is `HF_BUDGET_USD` minus the safety margin, rounded down, and is the **only** place the margin is applied.

### Quotas and atomicity

`limits.py` builds `usage.Quota` objects (`metric` ∈ `requests`, `attempts`, `images`, `cost_micros`; a ceiling; an amount to add; an optional provider family and window). `usage.py` evaluates them **inside the same `BEGIN IMMEDIATE` transaction that records the new request or attempt**, so simultaneous requests cannot both claim the same remaining quota or budget. Rolling windows (60 s) exclude an event exactly 60 s old; the daily window runs from local midnight in `QUOTA_TIMEZONE` (inclusive of midnight).

| Limit | Counts | Checked |
|---|---|---|
| `MAX_GENERATION_REQUESTS_PER_MINUTE` (3) | authorised requests in the last 60 s | at request start |
| `MAX_IMAGES_PER_DAY` (5) | images (see rule 3) per local day | at request start |
| `GEMINI_MAX_REQUESTS_PER_MINUTE` (5) / `_PER_DAY` (50) | every real Gemini attempt | at request start **and before every retry** |
| `HF_BUDGET_USD` (4.75 cumulative, or 5 monthly; minus 10%) | cost in microdollars (rule 6): all history, or the current month | at request start (whole batch) **and before every retry** |

### Budget modes (`HF_BUDGET_MODE`)

The Hugging Face budget has two modes, selected with environment variables (no code change). **Both use the same safety margin, reservations, retry accounting and atomic checks;** the only difference is *which recorded spending is added up*.

| | `cumulative` (default, **current**) | `monthly` (future) |
|---|---|---|
| `HF_BUDGET_MODE` | `cumulative` (or unset) | `monthly` |
| `HF_BUDGET_USD` means | the total you track, e.g. remaining prepaid credit | the allowance for each calendar month; **must be set explicitly** |
| Owner's configuration | `4.75` → usable **$4.275** | `5` → usable **$4.50 per month** |
| Safety margin | 10%, applied once in `Settings.hf_usable_budget_micros` | same |
| Spending counted | every attempt and adjustment ever recorded | attempts that **started**, and adjustments **entered**, since 00:00 on the 1st of the current month in `QUOTA_TIMEZONE` (`Asia/Jerusalem`) |
| Automatic reset | none (not on a new month, not on restart) | 00:00 on the 1st, Asia/Jerusalem time. Computed from timestamps at check time; no scheduler or cron |
| `retry_after_seconds` on a refusal | none (nothing frees it by itself) | seconds until the 1st |

**How it works.** `limits.budget_quota(settings, family, adding)` is the single definition of the budget check. It always uses `ceiling = settings.hf_usable_budget_micros` (the margin is not applied again) and sets `Quota.month_timezone` only in monthly mode. `usage.SqliteUsageStore._window` turns a month window into a UTC start time using `zoneinfo`, so daylight saving is handled (1 Nov 2026 00:00 Jerusalem is 2026-10-31 22:00 UTC; 1 Apr 2026 00:00 is 2026-03-31 21:00 UTC). `_committed_cost(conn, prefix, since_iso)` then adds `started_at >= since` to the attempt queries and `created_at >= since` to the adjustment query. With no window it adds up everything. `admin.py status` uses the same quota, so the report and the enforcement cannot disagree.

**What belongs to which month (monthly mode).**
- An attempt belongs to the month it **started** in. A retry after midnight counts in the new month; the first attempt stays in the old one. Each attempt is counted once.
- A confirmed actual cost (`admin.py record-actual`) replaces that attempt's estimate in the attempt's own month.
- An adjustment (`admin.py adjust`) belongs to the month in which it was **entered**.
- The unused reservation of a request that is still pending always counts (it is about to become attempts now), including across a month boundary.

**History is never deleted or rewritten.** The mode only changes which stored rows are summed. `admin.py status` prints `all recorded history` beside the window total so you can see both.

**What switching does.**

| Switch | Effect |
|---|---|
| cumulative → monthly | Earlier months stop counting; this month's spending (if any) still counts; the budget is available again up to the monthly ceiling |
| monthly → cumulative | **All** history counts again, including months monthly mode ignored. If that total is already over the ceiling, images are refused until credit is added and `HF_BUDGET_USD` is raised or a correction is recorded |

**Safe failure.** `HF_BUDGET_MODE` must be `cumulative` or `monthly` (case-insensitive); anything else, including a blank value, is a `ConfigError` at startup and no provider is called. Monthly mode **refuses to start unless `HF_BUDGET_USD` is written explicitly**, because its built-in default ($4.75) is a remaining balance, not a monthly allowance. An unset mode means `cumulative`, the stricter choice. Settings are cached per process, so a restart is needed after editing `.env`.

**Limit of the design.** The app never sees your real provider balance. In monthly mode `HF_BUDGET_USD=5` means "allow up to $4.50 of *estimated* spending per month", whether or not that much credit exists. The month boundary shares `QUOTA_TIMEZONE` with the daily image quota, so changing the time zone moves both. How to switch, step by step: DEVELOPMENT_GUIDE.md section 6. Tests: `tests/test_budget_modes.py`.

### Stale (crashed) requests

A request still `pending` with no activity for `STALE_REQUEST_SECONDS` (default 900; must be ≥ 240) is closed as `failed` / `abandoned` the next time any request is authorised **or any provider attempt (including a retry) is about to start** (or via `admin.py sweep-stale`). Its reservation is released; its unfinished attempts stay `started` and **keep counting as possibly billed**. The 240 s minimum is twice the longest provider timeout (HF 120 s), so a slow but live request is not mistaken for a crashed one. Timeouts are per network operation, not total wall time, which is why the margin is 2×.

### Money

All spending arithmetic uses **integer microdollars** (1 USD = 1,000,000). Settings and admin input are parsed as `Decimal` and rejected if they have more than 6 decimal places. There is no floating-point tolerance anywhere in budget code; the store refuses `float` money arguments. (The only `float` left in the codebase is the `Retry-After` header, which is seconds.)

---

## 10. Configuration (`config.py`)

Order: real environment variables > `.env` (loaded by `python-dotenv`) > built-in defaults. `.env` is git-ignored; `.env.example` has placeholders only. `config.get_settings()` validates everything once per process and caches the result (`lru_cache`), so **changing `.env` requires a restart**. A bad value raises `ConfigError` naming the setting; nothing is silently defaulted. `main.py` calls it at startup.

| Variable | Default | Notes |
|---|---|---|
| `API_KEY` / `HF_TOKEN` | none | secrets, read only through `config.require_secret()` right before a provider is used |
| `GENERATION_ENABLED` | `true` | global switch |
| `MAX_PROMPTS_PER_REQUEST` | `10` | |
| `MAX_IMAGES_PER_REQUEST` | `2` | must be ≤ `MAX_IMAGES_PER_DAY` |
| `MAX_CONCEPT_LENGTH` / `MAX_IMAGE_PROMPT_LENGTH` | `500` / `2000` | |
| `MAX_GENERATION_REQUESTS_PER_MINUTE` | `3` | |
| `MAX_IMAGES_PER_DAY` | `5` | |
| `QUOTA_TIMEZONE` | `Asia/Jerusalem` | IANA name; needs the `tzdata` package on Windows |
| `USAGE_DB_PATH` | `usage.db` | relative to the current directory |
| `GEMINI_PLAN` | `free` | only `free` is accepted |
| `GEMINI_MAX_REQUESTS_PER_MINUTE` / `_PER_DAY` | `5` / `50` | conservative guesses, **not** Google's published limits |
| `HF_BUDGET_MODE` | `cumulative` | `cumulative` or `monthly` (see *Budget modes* in section 9); anything else is an error |
| `HF_BUDGET_USD` | `4.75` | cumulative: total tracked. monthly: allowance per calendar month (must be set explicitly). Renamed from `HF_MONTHLY_BUDGET_USD`, which now errors |
| `HF_BUDGET_SAFETY_MARGIN_PERCENT` | `10` | usable ceiling = $4.275 by default |
| `HF_COST_PER_IMAGE_USD` | **none** | **required before images work**; the app does not guess a price |
| `STALE_REQUEST_SECONDS` | `900` | ≥ 240 |
| `IMAGE_PROVIDER` | `huggingface` | read directly in `generateImage.get_provider`, not through `config` |

Constants in `config.py` (not env vars): `GEMINI_TIMEOUT_SECONDS=60`, `HF_TIMEOUT_SECONDS=120`, `RETRY_WAIT_MAX_SECONDS=60`, `MIN_STALE_REQUEST_SECONDS=240` (derived).

## 11. Validation and error handling

`validation.py` functions and what they reject:

| Function | Error code |
|---|---|
| `ensure_generation_enabled` | `generation_disabled` |
| `validate_concept` | `empty_concept`, `concept_too_long` |
| `validate_prompt_count` | `invalid_prompt_count`, `too_many_prompts` |
| `validate_image_prompt` | `empty_image_prompt`, `image_prompt_too_long` |
| `validate_image_count` | `too_many_images` |
| `validate_unique_prompt_ids` | `duplicate_prompt_ids` |

Input is **never truncated**; over-limit input is refused. These checks run in `services.py` *and again* in the lowest-level provider functions.

`errors.py` family (each has `code`, `message`, `details`, `http_status`, `to_dict()`; a future FastAPI layer maps them to responses):

| Class | HTTP | Code |
|---|---:|---|
| `ValidationError` | 422 | as above |
| `GenerationDisabledError` | 503 | `generation_disabled` |
| `UsageUnavailableError` | 503 | `usage_unavailable` (the database couldn't be used, so generation did not start) |
| `LimitExceededError` | 429 | `rate_limited`, `daily_quota_exceeded`, `budget_exceeded`, `provider_rate_limited`, `provider_daily_limit`; has `retry_after_seconds` |
| `PricingUnavailableError` | 503 | `pricing_unavailable` |
| `AuthorizationRequiredError` | 500 | `authorization_required` |

`config.ConfigError` (a `RuntimeError`) is separate: invalid settings or a missing secret.

**Two reporting styles:** a *single* failed idea or image comes back as a **result** (`ExpandResult` / `ImageResult`, with `ok`, `error`, `error_code`). A *request-wide* refusal from `generate_images` or `expand_ideas` is **raised** as an `AppError`. `expand_idea` (singular) always returns a result.

Naming overlap to be aware of: the attempt category `rate_limited` (the *provider* answered HTTP 429) and the limit code `rate_limited` (the app's own per-minute request limit) are different things that share a string.

## 12. Credentials and logging

- Secrets are only read via `config.require_secret(name)`; the error names the variable but never shows a value.
- `logging_config.redact()` removes the configured keys and key-shaped strings (Bearer tokens, `hf_…`, `AIza…`, `Authorization:` / `api_key=` values). It is applied to the error strings returned by both provider modules and to every log record (`RedactingFilter`).
- Not everything passes through `redact()`: e.g. `services.py` builds `f"Unexpected error: {e}"` and `f"Could not process the model response: {e}"` from exceptions raised by file/JSON handling. Those are very unlikely to contain a key, but they are not scrubbed. Tracebacks are never redacted, so do not log exceptions with `exc_info`.
- The retry predicates in both provider modules use plain `print()` for "Retrying shortly..." messages (not the logging module).

## 13. Files on disk

```
input/batch.txt         sample batch file (one idea per line); other input/* is git-ignored
output/<concept>_<timestamp>.json      prompt files (git-ignored)
output/images/<concept>-prompt-<id>-<timestamp>[-n].png   images (git-ignored; never overwritten)
usage.db (+ usage.db-wal, usage.db-shm while running)    usage database (see CHECKPOINT_STATUS.md about .gitignore)
sample output/          tracked example prompt file + image used in README.md
```

File names use the idea cut to 80 characters; the full idea is kept inside the JSON. `output/…` and `usage.db` are relative to the directory you launch from.

## 14. Dependencies (`requirements.txt`)

| Package | Used for |
|---|---|
| `google-genai` | Gemini client, `APIError`, `HttpOptions` |
| `huggingface_hub` | `InferenceClient`, `InferenceTimeoutError` |
| `httpx` | transport-error types (and a dependency of the above) |
| `tenacity` | retry/backoff |
| `python-dotenv` | loads `.env` |
| `questionary` | terminal menus (needs a real console; see DEVELOPMENT_GUIDE.md) |
| `pillow` | image objects and PNG saving |
| `tzdata` | IANA time zones on Windows (needed by `QUOTA_TIMEZONE`) |
| `pytest` | tests (development only) |

The standard library does a lot of work: `sqlite3`, `decimal`, `zoneinfo`, `dataclasses`, `threading` (tests), `logging`.

## 15. Key design decisions (and why)

- **Fail closed.** If a request or attempt can't be recorded, or a price is unknown, the provider is not called.
- **Authorise the whole batch first.** A batch that can't be fully afforded never starts (chosen by the project owner because the maximum is 2 images).
- **One source of truth for money.** Costs come from attempt rows; reservations are used up by attempts rather than added to them.
- **Atomic checks.** Limits are checked and recorded in one transaction so concurrency can't oversubscribe.
- **Integer microdollars.** Budget ceilings are enforced exactly, with no tolerance.
- **Two budget modes, one mechanism.** Cumulative and monthly differ only in which stored rows are summed (a time window on the same query), so reservations, retry accounting and atomicity are identical; switching never deletes history.
- **No guessed prices.** `HF_COST_PER_IMAGE_USD` has no default; the app is an estimator, **not** a provider-side spending cap (billing can lag, and the app can't see everything).
- **Service layer is UI-free.** `services.py` has no `print`, `input`, `questionary` or API-key access (a test enforces this), so FastAPI can call it later.

## 16. Glossary

- **Request:** one logical operation (an idea expanded, or an image batch). One `requests` row.
- **Attempt:** one real call to a provider, including each retry. One `provider_attempts` row.
- **Reservation:** budget and daily images held for a pending request.
- **Observer / permit:** the object `services.py` hands the provider code; required to make a call.
- **Stale / abandoned:** a `pending` request with no activity past the threshold; presumed crashed.
- **Microdollar:** 1/1,000,000 of a USD; the unit all money arithmetic uses.
- **Provider family:** the part of a provider label before `/` (`huggingface` in `huggingface/fal-ai`).
