# Prompt Expander: Development Guide

How to set up, run, test, change and debug the project. Facts here were verified on **Windows 11, Python 3.14.6** unless marked *(not verified)*. See [ARCHITECTURE.md](ARCHITECTURE.md) for how the pieces fit together.

---

## 1. Setup

### 1.1 Install

From the project folder (note the spaces in the path, so quote it):

```powershell
cd "<path to your prompt-expander folder>"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

You never need to "activate" the venv: calling `.\.venv\Scripts\python.exe` directly works everywhere (PowerShell's execution policy can block `Activate.ps1`). Everything below uses that form.

**Verified:** a brand-new venv built from `requirements.txt` installs cleanly and the full suite passes (285 passed, 1 skipped at commit `6215628`; 326 passed, 1 skipped after the budget-modes change, re-verified on the fresh install). It resolved *newer* library versions than the original development venv (for example `google-genai` 2.34.0 vs 2.8.0), and the tests still pass on both. There is no lock file, so versions can drift; if something breaks after a reinstall, compare `pip list` with the versions in the commit history first.

`tzdata` is in `requirements.txt` on purpose: Windows has no time-zone database, and without it `QUOTA_TIMEZONE=Asia/Jerusalem` fails with `ZoneInfoNotFoundError`.

### 1.2 Configure

```powershell
copy .env.example .env
```

Then edit `.env`. It is git-ignored and must never be committed or pasted into a chat/assistant. Minimum for real use:

```
API_KEY=<your Gemini key>
HF_TOKEN=<your Hugging Face token>
HF_COST_PER_IMAGE_USD=<your estimate of one image call's cost in USD, e.g. 0.05>
```

(`0.05` is only an example format, **not** a real price. Look up your provider's pricing yourself.)

**Budget mode:** the budget is `cumulative` by default (your current $4.75 → $4.275). To switch to a monthly $5 → $4.50 budget later, see section 6, which has the exact steps.

**`HF_COST_PER_IMAGE_USD` is mandatory for images.** Without it the app refuses image generation with `pricing_unavailable` rather than run unmetered. At the time of writing, the owner's real `.env` has only `API_KEY` and `HF_TOKEN`, so this must be added before the first real image run. Prompt generation (Gemini) needs no price.

All other settings have defaults; the full list is in `.env.example`, `README.md` and ARCHITECTURE.md §10. Remember: settings are cached per process, so **restart after editing `.env`**.

Tests need **no** `.env`, no keys and no network: verified by running the suite from a clean `git archive` of HEAD with no `.env`.

---

## 2. Running the application

```powershell
.\.venv\Scripts\python.exe main.py
```

Run it **from the project folder**: `output/`, `input/` and `usage.db` are relative to the current directory. (`.env` is found regardless of where you launch, *verified* for script runs.)

**Use a real Windows console** (Windows Terminal, PowerShell or cmd). Run under Git Bash with piped input, `main.py` crashes with `prompt_toolkit.output.win32.NoConsoleScreenBufferError: Found xterm-256color...` *(verified)*. The interactive menus themselves have not been exercised end to end by the assistant that wrote this; the tests drive the CLI functions with mocked Questionary answers.

Menu: **Single Idea**, **Batch Mode** (a `.txt` file, one idea per line; try `input/batch.txt`), **Generate Images From Prompts**, **Exit**.

Typical first real session (do this with the owner's authorisation, as it spends provider quota/credits):
1. Set `HF_COST_PER_IMAGE_USD`.
2. `python admin.py status` to check the budget and limits.
3. Single Idea → a short idea → a JSON file appears in `output/`.
4. Generate Images → pick that file → tick 1 prompt → an image appears in `output/images/`.
5. `python admin.py status` again: one image used today, estimated spend equals one attempt's price (more if a retry happened).

### 2.1 Admin tool

```powershell
.\.venv\Scripts\python.exe admin.py status
.\.venv\Scripts\python.exe admin.py sweep-stale
.\.venv\Scripts\python.exe admin.py adjust --provider huggingface --amount=-1.25 --note "dashboard shows 1.25 less"
.\.venv\Scripts\python.exe admin.py record-actual --attempt-id 17 --usd 0.031
```

`status` output looks like this (verified, on a throwaway database):

```
Generation switch: ON (GENERATION_ENABLED)

Hugging Face budget: CUMULATIVE mode (counts all recorded history, never resets; the app's estimate, not your provider balance)
  usable ceiling       $4.2750  (HF_BUDGET_USD $4.75 in total minus 10% margin)
  committed            $0.0000
    reserved (active)  $0.0000
    estimated spent    $0.0000   (every attempt, failed ones included)
    confirmed actual   $0.0000
    adjustments        $0.0000
  remaining            $4.2750
  cost per image       $0.0500 (HF_COST_PER_IMAGE_USD)
  all recorded history $0.0000   (all of it is counted)

Images today (Asia/Jerusalem):         0 of 5
...
```

Use `--amount=-1.25` (with `=`) for negative numbers. Amounts are exact; more than 6 decimal places is an error.

---

## 3. Running the tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q                      # everything (about 20 s)
.\.venv\Scripts\python.exe -m pytest -q tests\test_usage.py  # one file
.\.venv\Scripts\python.exe -m pytest -q -k "budget"          # by name
.\.venv\Scripts\python.exe -m pytest -q -x                   # stop at first failure
```

Expected today: `344 passed, 1 skipped`. The skipped test is `test_live_hugging_face`, an opt-in live call that **spends real credits** and runs only with `RUN_LIVE_HF=1` and a real `HF_TOKEN`. Do not enable it without the owner's explicit permission.

### 3.1 How tests avoid real API calls and real money

`tests/conftest.py` has **autouse** fixtures, active in every test:

| Fixture | Effect |
|---|---|
| `block_real_api_calls` | replaces `InferenceClient.text_to_image` and `genai.Client` with functions that raise `AssertionError("Real API call attempted in a test; mock it.")`. (Skipped only when `RUN_LIVE_HF=1`.) |
| `no_wait` | removes retry back-off sleeps for the image provider |
| `default_settings` | sets every setting to the documented default, sets a made-up `HF_COST_PER_IMAGE_USD=0.05`, and points `USAGE_DB_PATH` at a throwaway file, so your `.env` and your real `usage.db` can't influence or be touched by tests; clears the settings cache before and after |

Opt-in helpers:

| Helper | Use |
|---|---|
| `settings(NAME=value, ...)` | change settings inside a test (also clears the cache) |
| `permit()` | an `AttemptObserver` on a throwaway store, for tests of low-level provider functions that refuse to run without one |
| `ScriptedProvider([...])` | fake image provider: plays back results/exceptions in order, counts `.calls`; reports itself as `huggingface/test` |
| `make_http_error(503)` | builds a real-looking Hugging Face HTTP error |
| `Clock` / `clock` fixture (`test_limits.py`) | a controllable fake time; pass `clock=` to `SqliteUsageStore` |
| `FakeGemini([...])`, `reply()`, `gemini_error(503)` (`test_usage.py`) | fake Gemini client and replies |

A quick manual check that nothing leaks: after a test run there must be no `usage.db` in the project folder (`ls *.db*` finds nothing). Verified.

### 3.2 What each test file covers

| File | Tests | Covers |
|---|---:|---|
| `test_config.py` | 45 | settings, credentials, `.env.example`, redaction, startup exit |
| `test_validation.py` | 46 | validation, generation switch, structured errors, CLI messages, long file names |
| `test_usage.py` | 50 | store persistence/lifecycle, attempts and retries via services, failed/partial/interrupted batches, fail-closed, Gemini flows, no secrets in the database |
| `test_limits.py` | 62 | pricing, daily/rate/provider limits, budget, exact money, stale requests, timeouts, per-image reservations, concurrency, admin tool, database upgrades, batch wait message |
| `test_budget_modes.py` | 41 | cumulative vs monthly budget: both ceilings, no reset in cumulative, reset at the Jerusalem month boundary (incl. daylight saving and year rollover), history kept across mode switches, invalid configuration, concurrency and retry accounting in both modes |
| `test_stale_retry_sweep.py` | 18 | the sweep before a retry's budget check, in both budget modes: crashed reservations don't block a retry, live requests are not swept, genuinely stale ones are still rejected, interrupted calls stay charged, concurrency |
| `test_services.py` | 25 | workflows with fake providers, file saving, statuses, key-free module check |
| `test_cli.py` | 10 | `main.py` functions with mocked Questionary |
| `test_generateImage_unit.py` | 41 | provider retry/error handling with fake providers |
| `test_generateImage_integration.py` | 7 | real provider class + real `InferenceClient`, network call replaced (+ the skipped live test) |

### 3.3 Writing a new test: patterns to copy

Image batch with a fake provider and an isolated store:

```python
def test_example(workdir, store):                 # workdir: chdir to tmp_path; store: throwaway SQLite
    provider = ScriptedProvider([make_http_error(503), Image.new("RGB", (4, 4))])   # fail once, then succeed
    results = services.generate_images(prompt_data(1), [1], provider=provider, store=store)
    assert results[0].ok and provider.calls == 2
    (row,) = store.list_requests()
    assert row["outcome"] == "succeeded"
    assert len(store.list_attempts(row["request_id"])) == 2     # the retry is a second attempt
```

Time-dependent behaviour: build the store with a fake clock and move it (`clock.advance(61)`).
Concurrency: see `run_threads([...])` in `test_limits.py` (a `threading.Barrier` so threads start together).

---

## 4. How to change or add things

Always follow the owner's workflow (HANDOFF.md §4): small steps, tests first or alongside, run the full suite, report, don't commit unless told.

### 4.1 Add a setting

1. `config.py`: add the name and default to `DEFAULTS`, a field to `Settings`, and a `read(...)` line in `load_settings` with the right parser (`_parse_int`, `_parse_bool`, `_parse_usd`, ...). A setting with no safe default (like `HF_COST_PER_IMAGE_USD`) is read separately and is `None` until set.
2. Add it to `.env.example` and the README table.
3. `tests/test_config.py`: a default test, an override test, invalid values (use the parametrized list). `conftest.default_settings` picks up new `DEFAULTS` automatically.

### 4.2 Add a new image provider

1. In `generateImage.py` subclass `ImageProvider`:

```python
class OtherProvider(ImageProvider):
    def generate(self, prompt):            # return a PIL image; raise on failure
        ...
    def is_retryable(self, e) -> bool:     # True only for transient errors
        ...
    def usage_identity(self):              # label stored in usage records; part before "/" is the family
        return "otherco/sdxl", "sdxl-1"
```

2. Register it: `PROVIDERS["other"] = OtherProvider`; select with `IMAGE_PROVIDER=other`.
3. **Pricing is mandatory:** in `pricing.py:call_cost_micros` add a branch for family `otherco` returning its per-call cost in **microdollars** from a new setting. Without it, `limits.image_plan` raises `PricingUnavailableError`.
4. **Budget caveat:** `limits.image_plan` applies the Hugging Face budget (`settings.hf_usable_budget_micros`) to *whatever* provider family the image provider reports, because there is only one image budget setting today. Spending is counted separately per provider family, but a second paid image provider would be compared against the *same ceiling number* as Hugging Face, which is almost certainly wrong for it. Give it its own budget setting and `Quota` instead. This is the one place the code is currently Hugging Face-shaped.
5. Tests: reuse `ScriptedProvider` as the template (see `test_swapping_in_another_provider`).

### 4.3 Add or change a limit

Limits are built in `limits.py` as `Quota` objects. Pick a `metric` (`requests`, `attempts`, `images`, `cost_micros`), a `ceiling`, `adding`, an optional `provider_prefix`, and a window (`window_seconds` or `day_timezone`). Put the quota in the `Plan` as `begin_quotas` (checked once when the request starts) and/or `attempt_quotas` (checked again before every provider call). A genuinely new *kind* of thing to count needs a new branch in `usage.SqliteUsageStore._measure`. Add tests for it in `test_limits.py`, including a concurrency test like `test_concurrent_requests_cannot_reserve_the_same_budget`.

### 4.4 Add an error code

Create/raise `ValidationError("my_code", "User-facing message", detail=...)` or `LimitExceededError("my_code", ...)`. Add the code to the table in ARCHITECTURE.md §11. The CLI already prints `e.message` for any `AppError`.

### 4.5 Add a CLI menu item

Put the logic in a function in `services.py` that takes plain arguments and returns a result object (no `print`/`input`). Add a thin `process_*` function in `main.py` that asks questions, calls it, catches `AppError` and prints. Add the choice to `main_menu`. Keep `test_services_module_is_ui_and_key_free` passing.

### 4.6 Change the database

1. Add columns to `_SCHEMA` (fresh databases) **and** to `_ADDED_COLUMNS` (upgrade path) in `usage.py`.
2. Bump `SCHEMA_VERSION`.
3. If old data needs converting, extend `_upgrade`.
4. Add a test that hand-builds an old-format file and opens it (see `test_version_1_database_is_upgraded_in_place`).

### 4.7 Change the model or the price

- Gemini model: `GEMINI_MODEL` in `generatePrompts.py`.
- Hugging Face model/route: `HuggingFaceImageProvider.__init__` defaults. Then **re-check `HF_COST_PER_IMAGE_USD`**, since one price is applied to any model.
- Never hard-code a price in code. Prices belong in settings, consumed only via `pricing.py`.

### 4.8 Code conventions observed in this repo

- Newer modules use `snake_case`; the original `fileUtils.py` uses `camelCase` (`saveJSON`, `getOutputFiles`). Match the file you are in; don't mass-rename.
- Comments explain *why*, not *what*. Module docstrings state the rules a module guarantees.
- Money is always integer microdollars inside; `Decimal`/text only at the edges (`money.py`).
- Expected failures are returned as results or raised as `AppError`; `services.py` never prints.
- Tests are plain pytest functions with `# ----` section headers. No test may call a real provider.

---

## 5. Debugging common problems

| Symptom (message you'll see) | Cause | Fix |
|---|---|---|
| `The cost of an image from huggingface/fal-ai is not configured...` (`pricing_unavailable`) | `HF_COST_PER_IMAGE_USD` not set | add it to `.env`, restart |
| `Too many generation requests: 3 in the last minute` (`rate_limited`) | per-minute request limit | wait; batch mode waits automatically and prints `[Waiting] ...` |
| `Daily image limit reached: 5 of 5 ...` (`daily_quota_exceeded`) | `MAX_IMAGES_PER_DAY` used (counts images, not retries) until local midnight (`Asia/Jerusalem`) | wait, or raise the limit |
| `The huggingface budget would be exceeded...` (cumulative) or `The huggingface monthly budget would be exceeded...` (`budget_exceeded`) | estimated spend + reservation would pass the usable ceiling (all history in cumulative mode, this month in monthly mode) | `admin.py status`; compare with the provider dashboard; raise `HF_BUDGET_USD` if you added credits, or use `admin.py adjust` (README "Restoring generation"); in monthly mode it clears by itself on the 1st |
| `The Gemini request limit for this minute...` (`provider_rate_limited`) / `...daily request limit...` (`provider_daily_limit`) | the app-side Gemini caps (guessed defaults 5/min, 50/day) | wait, or tune `GEMINI_MAX_REQUESTS_PER_*` to your real quota |
| `Generation is temporarily unavailable` | `GENERATION_ENABLED=false` | set `true`, restart |
| `Usage tracking is unavailable...` (`usage_unavailable`) | the database file can't be opened/written (bad path, permissions, locked >10 s) | check `USAGE_DB_PATH`; generation deliberately does not start without recording |
| `Invalid configuration: - NAME ...` at startup | a setting failed validation | fix that line in `.env` |
| `HF_MONTHLY_BUDGET_USD was renamed to HF_BUDGET_USD` | old variable name in `.env` | rename it |
| `API_KEY is not set` / `HF_TOKEN is not set` | missing credential | add to `.env` |
| `Generation must be started through the service layer` | a provider function was called without `services.py` | call `services.expand_idea` / `generate_images` instead (or `permit()` in a test) |
| `ZoneInfoNotFoundError` | `tzdata` not installed | `pip install -r requirements.txt` |
| `NoConsoleScreenBufferError` | running `main.py` under Git Bash/mintty or with piped input | use Windows Terminal / PowerShell / cmd |
| Inference "credits exhausted" (HTTP 402) | the Hugging Face account is out of credit | add credit; this is the *provider's* balance, separate from the app's estimate |
| A request stuck as `pending` in `admin.py status` after a crash | process died mid-request | `admin.py sweep-stale` (it also happens automatically after `STALE_REQUEST_SECONDS`) |
| Edited `.env` but nothing changed | settings are cached per process | restart |

### 5.1 Looking inside the usage database

Save as `inspect_usage.py` (do not commit it) and run it with the venv Python from the project folder:

```python
import sqlite3
conn = sqlite3.connect("usage.db")
conn.row_factory = sqlite3.Row
for r in conn.execute("SELECT created_at, operation, outcome, items_succeeded, items_requested, "
                      "error_category, estimated_cost_micros FROM requests ORDER BY created_at DESC LIMIT 20"):
    print(dict(r))
print("--- attempts of the newest request:")
newest = conn.execute("SELECT request_id FROM requests ORDER BY created_at DESC LIMIT 1").fetchone()
for a in conn.execute("SELECT item_ref, attempt_number, status, error_category, http_status, "
                      "estimated_cost_micros FROM provider_attempts WHERE request_id = ?", (newest[0],)):
    print(dict(a))
```

Costs are in microdollars (50000 = $0.05). Open it read-only if the app is running.

### 5.2 Debugging habits that work here

- Reproduce with a fake first: write a failing test using `ScriptedProvider` / `FakeGemini` / a fake `Clock`, then fix. Almost every behaviour in the app has a test of that shape to copy.
- To see *why* a limit refused something, read `admin.py status` and the `rejected` rows in `requests` (`error_category` holds the code).
- A mismatch between the app's estimate and your provider bill is expected: the app estimates, charging every attempt the configured price. Use `admin.py adjust` / `record-actual` to correct it.

---

## 6. Switching the Hugging Face budget mode

The budget mode is chosen with environment variables in `.env`, so changing it never needs a code change. Switching **never deletes or alters usage records**: the mode only decides which stored spending is added up.

### 6.1 The settings

| Variable | Cumulative (your current setup) | Monthly (the future setup) |
|---|---|---|
| `HF_BUDGET_MODE` | `cumulative` (also what you get if the line is missing) | `monthly` |
| `HF_BUDGET_USD` | `4.75`: your remaining Hugging Face credit, in total | `5`: your allowance **per calendar month**. Must be written explicitly in monthly mode |
| `HF_BUDGET_SAFETY_MARGIN_PERCENT` | `10` | `10` |
| **Usable ceiling** (computed for you) | **$4.275** | **$4.50 per month** |
| What counts against the ceiling | all spending ever recorded | spending since 00:00 on the 1st of the current month, Asia/Jerusalem time |
| Resets | never: not on a new month, not on restart | automatically, at 00:00 on the 1st (Asia/Jerusalem) |

Put the **full** amount ($4.75 or $5) in `HF_BUDGET_USD`. The 10% margin is applied exactly once, when the ceiling is computed, so don't subtract it yourself.

Month boundaries use `QUOTA_TIMEZONE` (default `Asia/Jerusalem`, the same setting as the daily image quota) and follow daylight saving: 1 November 00:00 in Jerusalem is 22:00 UTC on 31 October, while 1 April 00:00 is 21:00 UTC on 31 March.

### 6.2 Your current configuration

Your `.env` currently has no `HF_BUDGET_*` lines (it holds only `API_KEY` and `HF_TOKEN`), so the defaults apply: **cumulative mode, $4.75, 10% margin, usable $4.275**. That is the configuration you asked for. To make it explicit (recommended, so nobody has to guess), add these lines to `.env`:

```
HF_BUDGET_MODE=cumulative
HF_BUDGET_USD=4.75
HF_BUDGET_SAFETY_MARGIN_PERCENT=10
```

Restart (6.3, step 2) and run `.\.venv\Scripts\python.exe admin.py status`. You should see `CUMULATIVE mode` and `usable ceiling       $4.2750`.

### 6.3 Switching from cumulative to monthly: exact steps

1. **Look at where you are** (optional): `.\.venv\Scripts\python.exe admin.py status`. Note `committed` and `all recorded history`.
2. **Stop the app.** Choose *Exit* in the menu, or press Ctrl+C. Settings are read once when the app starts, and there is no background service, so "restart" just means quitting and launching again. (`admin.py` reads the settings fresh every time you run it.)
3. **Edit `.env`** in the project folder (VS Code or Notepad). Set these three lines, replacing existing ones or adding them:
   ```
   HF_BUDGET_MODE=monthly
   HF_BUDGET_USD=5
   HF_BUDGET_SAFETY_MARGIN_PERCENT=10
   ```
   Leave `API_KEY`, `HF_TOKEN` and `HF_COST_PER_IMAGE_USD` alone. No quotes and no `$` sign.
4. **Start again:** `.\.venv\Scripts\python.exe main.py` (in a real console). If a value is wrong, the app refuses to start and names it (6.6).
5. **Verify before generating anything:** `.\.venv\Scripts\python.exe admin.py status`. Check that:
   - the headline says `MONTHLY mode`;
   - `usable ceiling` is `$4.5000  (HF_BUDGET_USD $5.00 per month minus 10% margin)`;
   - `committed` shows only this month's spending;
   - `all recorded history` still shows the earlier spending, marked `(kept; only this month is counted)`.

### 6.4 Worked example (real output)

Suppose that in October you spent six images at an estimated $0.4275 each ($2.565) under the cumulative setup, and it is now November. The output below was produced by running the real `admin.py status` against a throwaway database containing exactly that history (only the relevant lines are shown; the app's own database was not touched).

**Before:** `HF_BUDGET_MODE=cumulative`, `HF_BUDGET_USD=4.75`

```
Hugging Face budget: CUMULATIVE mode (counts all recorded history, never resets; ...)
  usable ceiling       $4.2750  (HF_BUDGET_USD $4.75 in total minus 10% margin)
  committed            $2.5650
  remaining            $1.7100
  all recorded history $2.5650   (all of it is counted)
```

**After** editing `.env` to `HF_BUDGET_MODE=monthly` and `HF_BUDGET_USD=5`, then restarting:

```
Hugging Face budget: MONTHLY mode (counts the current calendar month in Asia/Jerusalem; resets on the 1st; ...)
  usable ceiling       $4.5000  (HF_BUDGET_USD $5.00 per month minus 10% margin)
  committed            $0.0000
  remaining            $4.5000
  all recorded history $2.5650   (kept; only this month is counted)
```

October's $2.565 is **not lost**: it is still stored (`all recorded history`), but it belongs to October, so it no longer counts against November's $4.50. On 1 December at 00:00 Jerusalem time, November's spending stops counting in the same way, automatically.

### 6.5 Switching back to cumulative

Set `HF_BUDGET_MODE=cumulative` and `HF_BUDGET_USD` to your real remaining credit (for example `4.75`), then restart. **All** history counts again, including the months monthly mode ignored. If that total is already over the new ceiling, image generation is refused until you add credit and raise `HF_BUDGET_USD`, or record a correction (README, "Restoring generation"). That is deliberate: it is the safe direction.

### 6.6 If the configuration is wrong

The app fails safely: it **refuses to start** (or `admin.py` prints the error and exits with code 1) and no provider is called. These are real messages:

```
[Error] Invalid configuration:
  - HF_BUDGET_MODE must be one of cumulative, monthly, got 'monthy'.
```

```
[Error] Invalid configuration:
  - HF_BUDGET_MODE=monthly requires HF_BUDGET_USD to be set explicitly (the built-in default is a remaining balance, not a monthly allowance).
```

A missing `HF_BUDGET_MODE` line means `cumulative` (the stricter choice). An unusable `HF_BUDGET_USD` (zero, negative, text, or more than 6 decimal places) is refused in both modes.

### 6.7 Things to know

- **Nothing is deleted.** Never edit `usage.db` by hand. To correct the app's figures, use `admin.py adjust` or `admin.py record-actual`; they are recorded with your note.
- **The app cannot see your real balance.** Monthly mode means "allow up to $4.50 of *estimated* spending per month", whether or not that much credit exists. If your real credit is lower, the provider will refuse (HTTP 402) before the app does. Only switch to monthly when you really are funding about $5 a month.
- **An adjustment belongs to the month it was entered in** (monthly mode). A correction for October entered in November counts in November.
- **A request running at midnight:** each attempt counts in the month it started, and the unused reservation of a live request counts in the new month.
- **Changing `QUOTA_TIMEZONE` moves both** the daily image quota and the month boundary.
- **Verify without spending anything:** `.\.venv\Scripts\python.exe -m pytest -q tests\test_budget_modes.py` (fake providers, fake clock).

## 7. Git workflow (the owner's rules)

- The owner reviews and commits. **Do not run `git commit`, `git push`, `git reset` or any destructive Git command.** Never discard or modify pre-existing uncommitted work without asking.
- Never stage `.env`, `*.db`, `usage.db-wal`, `usage.db-shm`, `output/` contents, or unrelated files. Suggest explicit `git add <files>` commands instead of `git add .`.
- Note: `.gitignore` ignores `*.db` but **not** `usage.db-wal` / `usage.db-shm` (verified with `git check-ignore`), which SQLite creates while the app runs. Don't stage them (see CHECKPOINT_STATUS.md, outstanding concerns).
- A GitHub secret-scanning alert once fired on fake key-shaped test strings. The test fakes are now assembled from fragments (`"hf" + "_" + "FAKE" * 4 + ...`) so no complete key-shaped literal is in the source. Keep it that way in new tests.
