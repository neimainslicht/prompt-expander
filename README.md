# Prompt Expander

A Python application that transforms short, simple prompts into detailed, high-quality prompts for generative AI tools. This app also allows users to select prompts for image generation. This project helps users improve prompt quality for image generation, text generation, and other AI workflows.

## Features

- Expand short prompts into richer, more descriptive prompts
- Save expanded prompts to files for future use
- Batch process multiple prompts at once
- Organize outputs automatically
- Option to select prompts and generate images from them
- Command-line interface for quick usage

## Architecture

- `main.py` is the command-line interface only: it asks questions, calls `services`, and prints results.
- `services.py` holds the workflows (expand ideas, list/load prompt files, generate images). It takes plain inputs, returns result objects, and never prompts, prints, or reads API keys. A future web backend should import this module.
- `generatePrompts.py` (Gemini) and `generateImage.py` (image providers) are the only modules that read `API_KEY` / `HF_TOKEN` from `.env`. Keys and provider-specific code stay on the backend and must never be sent to a browser.

## Setup and configuration

```text
pip install -r requirements.txt
copy .env.example .env      (then edit .env)
python main.py
python -m pytest            (no API keys needed, no credits spent)
```

All settings live in `config.py` and are read from environment variables, with `.env` as a fallback. `.env` is git-ignored; `.env.example` holds placeholders only. Invalid values (non-numbers, zero or negative limits, an unknown time zone, a per-request image limit above the daily limit) stop the app at startup with a message naming the setting. They are never silently replaced by a default. Missing API keys are reported when a provider is first used, not at import.

| Variable | Default | Meaning |
|---|---|---|
| `API_KEY` | none | Gemini key (secret) |
| `HF_TOKEN` | none | Hugging Face token (secret) |
| `GENERATION_ENABLED` | `true` | Global switch for new generation (`true`/`false`) |
| `MAX_PROMPTS_PER_REQUEST` | `10` | Prompts per Gemini request |
| `MAX_IMAGES_PER_REQUEST` | `2` | Images per request |
| `MAX_CONCEPT_LENGTH` | `500` | Characters in an idea |
| `MAX_IMAGE_PROMPT_LENGTH` | `2000` | Characters in an image prompt |
| `MAX_GENERATION_REQUESTS_PER_MINUTE` | `3` | Generation requests per minute |
| `MAX_IMAGES_PER_DAY` | `5` | Images per day |
| `QUOTA_TIMEZONE` | `Asia/Jerusalem` | IANA time zone where the daily quota resets at midnight |
| `USAGE_DB_PATH` | `usage.db` | SQLite file holding usage records (git-ignored) |
| `STALE_REQUEST_SECONDS` | `900` | A request left pending with no activity this long (seconds) is treated as abandoned by a crash. Must be at least 240, twice the longest provider timeout |
| `GEMINI_PLAN` | `free` | Only `free` is accepted: Gemini is limited by request counts, not spending |
| `GEMINI_MAX_REQUESTS_PER_MINUTE` / `_PER_DAY` | `5` / `50` | Free-tier request caps. Conservative app-side values, not Google's published limits: check yours in AI Studio |
| `HF_BUDGET_MODE` | `cumulative` | How the Hugging Face budget is counted: `cumulative` (all recorded history, never resets) or `monthly` (the current calendar month in `QUOTA_TIMEZONE`, resets on the 1st). Anything else is an error |
| `HF_BUDGET_USD` | `4.75` | Cumulative mode: the total you track, e.g. remaining credit. Monthly mode: the allowance per calendar month, which **must be written explicitly**. At most 6 decimal places (renamed from `HF_MONTHLY_BUDGET_USD`) |
| `HF_BUDGET_SAFETY_MARGIN_PERCENT` | `10` | Share of that budget held back, so the app plans to spend at most 90% |
| `HF_COST_PER_IMAGE_USD` | none | **Required for images.** Your estimate of what one image call costs, in USD, at most 6 decimal places. Not guessed by the app |

Gemini (free tier) and Hugging Face (prepaid credits) are configured separately: Gemini is limited by request counts and is not treated as spending, while Hugging Face has a dollar budget. The app's figures are its own estimates and will not exactly match your provider balance; the provider's dashboard is the source of truth.

**Status:** all limits are enforced: request sizes, the generation switch, per-minute request limits, the daily image quota, Gemini free-tier request caps and the Hugging Face budget.

**Before generating images you must set `HF_COST_PER_IMAGE_USD`** in `.env`. The app does not know what your provider charges, and it refuses paid generation rather than run unmetered. (Writing prompts with Gemini needs no price.)

### Request limits and the generation switch
Checks live in `validation.py` and run before any provider is called. Over-long or oversized input is rejected with a message, never truncated.

- Empty or whitespace-only ideas, ideas over `MAX_CONCEPT_LENGTH` (measured without surrounding whitespace), and requests for more than `MAX_PROMPTS_PER_REQUEST` prompts are refused. Gemini is asked for exactly the configured number of prompts, and extra prompts it returns are dropped.
- Choosing more than `MAX_IMAGES_PER_REQUEST` images, or any chosen image prompt that is empty or over `MAX_IMAGE_PROMPT_LENGTH`, refuses the whole request, so nothing is generated. In the menu, the prompt picker shows the limit and won't accept too many.
- `GENERATION_ENABLED=false` stops all new Gemini and image calls and shows "Generation is temporarily unavailable". Listing and loading existing prompt files still works. The setting is read each time a request is authorized (and again at the lowest level, before each provider call), but it is loaded once per run, so restart the app after changing it. It does not cancel a batch already running.
- Refusals are `errors.AppError` subclasses with a `code`, `message` and `http_status` (422 for validation, 503 when disabled), so a future web layer can turn them into HTTP responses. A single idea or image that fails comes back as a result with `error_code`; request-wide refusals from `generate_images` and `expand_ideas` are raised.
- File names are shortened to 80 characters of the idea; the full idea is kept inside the saved JSON.

### Credential safety
Keys are read only through `config.require_secret()` right before a provider is used. Error messages and logs go through `logging_config.redact()`, which removes the configured keys and key-shaped strings (Bearer tokens, `hf_...`, `AIza...`, `Authorization:`/`api_key=` values). Logging records exception tracebacks unredacted, so don't log exceptions with `exc_info`.

### Usage tracking
Every generation request is recorded in a local SQLite file (`USAGE_DB_PATH`, default `usage.db`, git-ignored) by `usage.py`. Code depends on the `UsageStore` interface, so the SQLite implementation can later be replaced (for example by PostgreSQL).

Two tables, linked by `request_id`:

- `requests`: one row per logical request (one idea expanded, or one batch of images): id, `user_id` (`local` until there is authentication), UTC timestamps, operation (`prompt_generation` / `image_generation`), provider and model, items requested and succeeded, outcome (`pending`, `succeeded`, `partially_succeeded`, `failed`, `rejected`), estimated and actual cost columns, and a short error category.
- `provider_attempts`: one row per real provider call, retries included. Each has its request, the item (the prompt id in a batch), an attempt number (1, 2, 3 for retries of the same item), status, error category, HTTP status and, for Gemini, token counts.

How it behaves:

- A retry adds an attempt to the same request; it never creates a second request. The attempt row is written before the call and updated after it, so a call that was cut off (Ctrl+C, crash) stays as `started` and is treated as possibly billed.
- Usage is counted from `provider_attempts` only. A rejected request (too long, too many images, generation off) is stored for diagnostics with outcome `rejected` and no attempts, so it counts as no usage and no spending.
- A failed call is recorded, not dropped, because a failed call may still be billed. A save failure after a successful image call is recorded as a failed image but a succeeded attempt.
- If a request or an attempt can't be recorded, the call is not made (generation fails closed). A failure to record a result after a call is only logged.
- Every attempt stores the estimated cost it was charged (see 'Quotas and budget'); a confirmed actual cost can be added later with `admin.py`.
- Stored: ids, counts, timestamps, model names, token counts, error categories. Never stored: API keys, prompt text, raw provider error messages.

To look at recent usage:

```text
python -c "import usage; [print(r['created_at'], r['operation'], r['outcome'], r['items_succeeded'], '/', r['items_requested'], r['error_category']) for r in usage.get_store().list_requests(20)]"
```

A request left `pending` after a crash is closed as `failed` / `abandoned` the next time a request is authorised (or with `python admin.py sweep-stale`). Its unfinished attempts stay recorded and keep counting as possibly billed. See 'Quotas and budget'.

### Quotas and budget
Limits are checked, and the reservation recorded, in one atomic database step (`BEGIN IMMEDIATE`) before any provider is called, so simultaneous requests cannot both claim the same remaining quota or budget. The limits are built in `limits.py`; all pricing assumptions are in `pricing.py`. Enforcement sits at the service boundary: the low-level functions in `generatePrompts.py` / `generateImage.py` refuse to call a provider unless `services` has authorised the request.

| Limit | What it counts |
|---|---|
| `MAX_GENERATION_REQUESTS_PER_MINUTE` | Authorised requests in the last 60 seconds (an idea, or an image batch). Retries are not new requests. Rejected requests don't count |
| `MAX_IMAGES_PER_DAY` | **Images, not attempts.** Per calendar day in `QUOTA_TIMEZONE` (default `Asia/Jerusalem`), resetting at local midnight; restarts don't reset it. A running request counts the images it reserved; a finished one counts the images that got a provider attempt, so images never attempted are released. Retries of one image are never extra images. A request counts toward the day it started |
| `GEMINI_MAX_REQUESTS_PER_MINUTE` / `_PER_DAY` | **Every real Gemini call, retries included**, checked again before each retry |
| `HF_BUDGET_USD` (after the margin) | Hugging Face spend against `Settings.hf_usable_budget_micros`, so the margin is applied once. `cumulative`: all recorded spending, no reset ($4.75 gives $4.275 usable). `monthly`: spending since local midnight on the 1st in `QUOTA_TIMEZONE` ($5 gives $4.50 usable per month). History is never deleted either way |

**Budget modes.** `HF_BUDGET_MODE` chooses what the budget counts; nothing else changes (same margin, reservations, retry accounting and atomic checks). To change mode, edit `.env` and restart; the exact steps are in DEVELOPMENT_GUIDE.md section 6. Switching never deletes usage records: the mode only decides which stored attempts and adjustments are added up.

A refused request raises or returns a `LimitExceededError` (HTTP 429 for a web layer) with a message and, for rate limits, `retry_after_seconds`. In batch mode the app waits out a per-minute limit instead of failing the remaining ideas, and prints a `[Waiting] ...` line first so it doesn't look frozen.

**Costs and budget.** Hugging Face does not report what a call cost, so the app charges `HF_COST_PER_IMAGE_USD` for **every real attempt, including retries and failed or interrupted calls**, since a provider may bill for those. Three figures are kept apart: *reserved* (held for requests still running), *estimated spent* (attempts), and *confirmed actual* (entered by an administrator, replacing the estimate for that attempt).

*Exact money.* Settings are read as exact decimals (more than 6 decimal places is an error, never rounded), and all arithmetic and comparisons use whole microdollars (1 USD = 1,000,000), stored as integers. There is no floating point and no tolerance: a budget of $0.30 holds exactly three $0.10 images, and one microdollar over is refused. The usable budget is `HF_BUDGET_USD` minus the margin, rounded down to a whole microdollar, and is computed once in `Settings.hf_usable_budget_micros`.

*How an image batch is reserved.* Before the first call, the whole batch is authorised: its daily images and **exactly one attempt per image** are reserved together, so a batch that can't be fully afforded never starts. Each image's share of the reservation belongs to that image alone. When the image gets its first attempt, the share is replaced by the attempt (so it is not counted twice). **Every retry needs additional budget**, checked just before the retry is made; a retry can never use another image's reservation, and one that would pass the ceiling is not made. Each attempt is a single record that costs once, no matter how many times its result is reported. When a request finishes, shares for images that never ran are released, and only real attempts remain. An attempt that was cut off (Ctrl+C, crash) stays recorded as `started` and keeps its estimated cost. Each prompt can be selected only once per request. Gemini (free tier) costs nothing and is limited only by the request caps above.

**Stale requests and provider timeouts.** A request that has had no activity for `STALE_REQUEST_SECONDS` is assumed to have been killed by a crash. It is closed as `abandoned`, its reservation is released, and its unfinished attempts keep counting as possibly billed. Activity is recorded when a provider call starts and when it ends, so the longest silence in a healthy request is one call or one retry pause. To keep a slow but live request from being mistaken for a crashed one:

- every provider call has an explicit timeout: Gemini 60 seconds, Hugging Face 120 seconds, and retry pauses are capped at 60 seconds (all in `config.py`, and the providers read them from there);
- `STALE_REQUEST_SECONDS` must be at least twice the longest of these (240 seconds), and the app refuses to start with a smaller value;
- the default is 900 seconds, far above that.

Timeouts are per network operation, so a call can run somewhat longer than its timeout in rare cases; the 2x margin is there for that. A request that is still making progress is never touched.

These are the application's own estimates, not a spending cap at the provider: provider billing can lag, can include charges the app can't see, and the app can't know the real price unless you keep `HF_COST_PER_IMAGE_USD` correct. Keep a spending limit on the provider account as well.

### Restoring generation
When a request is refused for the budget or a quota:

1. Run `python admin.py status`. It shows the budget (committed, reserved, estimated, confirmed, adjustments), today's images, and recent requests.
2. Compare with your provider's dashboard, then do one of:
   - **Added credits:** raise `HF_BUDGET_USD` in `.env` and restart (in `cumulative` mode it is the total tracked since the start; in `monthly` mode it is the per-month allowance).
   - **The app's estimate is too high or low:** record a correction, kept with your note: `python admin.py adjust --provider huggingface --amount=-1.25 --note "dashboard shows 1.25 less"` (negative lowers recorded spend; amounts are exact, up to 6 decimal places).
   - **Known real cost of a call:** `python admin.py record-actual --attempt-id 17 --usd 0.031`.
   - **Stuck request after a crash:** `python admin.py sweep-stale`.
3. The daily image quota resets at midnight in `QUOTA_TIMEZONE`; to change the limit, edit `MAX_IMAGES_PER_DAY`.
4. To stop all generation at once, set `GENERATION_ENABLED=false`.

### Testing without spending
`python -m pytest` uses fake providers, a fake clock and a made-up test price, with a throwaway usage database. It never calls a real API.

### Limits of this local implementation
- One SQLite file: safe for several threads or processes on one machine, but not for several servers. A multi-instance deployment needs a shared database (for example PostgreSQL, behind the same `UsageStore` interface), with the atomic checks done as database transactions there.
- Rate limits are counted from the database, so they survive restarts, but they are per-installation, not per-user. Per-user limits need authentication (`user_id` is already stored).
- In-process code is trusted: the guard against calling providers directly stops mistakes and accidental bypasses, not deliberately malicious code in the same process.

## Examples

### Text to Prompts:

### Input

```text
ad for shakespeare in the park
```

### Output
 
```json
{
  "timestamp": "2026-08-11T14:38:42",
  "concept": "ad for shakespeare in the park",
  "prompts": [
    {
      "id": 1,
      "text": "Subject: A diverse group of people (families, couples, friends) picnicking on blankets, laughing, and looking towards an outdoor stage in a lush park. Composition: Wide shot, capturing the expanse of the park, the audience in the foreground, and a glimpse of actors on a simple wooden stage in the mid-ground, framed by large oak trees. Lighting: Golden hour, warm and soft sunlight casting long, gentle shadows. Camera Angle: Slightly elevated, looking down gently on the audience, eye-level with the stage. Mood: Joyful, communal, relaxed, inviting, magical. Style: Editorial photography, naturalistic, slightly idealized.",
      "status": "not generated"
    },
    {
      "id": 2,
      "text": "Subject: A lone actor, dressed as Hamlet in intricate period costume, stands dramatically on a simple outdoor stage made of reclaimed wood, holding a skull. Composition: Medium shot, rule of thirds, Hamlet slightly off-center with a large, ancient oak tree branch subtly framing the top of the scene. Lighting: Twilight, with dramatic stage spotlights creating sharp shadows and highlights on his face and costume, a soft blue ambient light from the darkening sky. Camera Angle: Low angle, looking up at Hamlet, emphasizing his stature and the intensity of the moment. Mood: Intense, contemplative, powerful, theatrical. Style: Cinematic, dark academia aesthetic, dramatic stage photography.",
      "status": "not generated"
    },
    {
      "id": 3,
      "text": "Subject: Puck, with a mischievous grin and a costume woven with leaves and moss, leaping gracefully mid-air between two ancient, gnarled trees in a dimly lit, enchanted park. Composition: Dynamic, full body shot capturing movement, with a winding forest path leading the eye towards him. Lighting: Ethereal moonlight filtering through dense leaves, augmented by hidden, warm fairy lights woven into the trees. Camera Angle: Eye-level, slightly upward to catch the apex of his leap. Mood: Whimsical, magical, enchanting, playful. Style: Fantasy art, dreamlike, vibrant but mysterious.",
      "status": "not generated"
    },
    {
      "id": 4,
      "text": "Subject: The silhouette of a small outdoor stage against a vibrant sunset, with a few actors mid-pose, their figures rendered in dark contrast. In the foreground, the out-of-focus heads of a diverse audience watching intently, creating a sense of immersion. Composition: Wide shot, strong negative space of the richly colored sky, stage silhouetted on the horizon. Lighting: Sunset, rich oranges, purples, and deep blues. Stage lights begin to glow subtly, creating a rim light on the silhouetted figures. Camera Angle: From behind the audience, slightly low, as if sitting on a picnic blanket. Mood: Serene, captivating, communal, nostalgic. Style: Atmospheric, impressionistic, romantic.",
      "status": "not generated"
    },
    {
      "id": 5,
      "text": "Subject: A group of young, energetic actors in vibrant, contemporary-meets-Renaissance costumes, laughing and interacting playfully on a small grassy knoll that serves as a stage in a sunny park. Children in the foreground are smiling and clapping. Composition: Medium-wide shot, dynamic grouping of actors, children in the lower third. Lighting: Bright, natural daylight, clear blue sky, warm sunshine highlighting the greens of the park. Camera Angle: Eye-level, slightly dynamic to capture movement and interaction. Mood: Joyful, energetic, accessible, fun, family-friendly. Style: Bright editorial, lively, slightly candid.",
      "status": "not generated"
    },
    {
      "id": 6,
      "text": "Subject: An elegant, stylized depiction of Shakespeare's classic profile, seamlessly merged with the silhouette of a sprawling park tree, with subtly overlaid text 'Shakespeare in the Park' integrated into the branches. Composition: Symmetrical, minimalist, clean lines creating a striking visual. Lighting: Soft, diffused daylight, perhaps early morning light with a gentle mist in the background, giving an ethereal quality. Camera Angle: Straight on, graphic perspective for a strong poster aesthetic. Mood: Sophisticated, timeless, elegant, artistic. Style: Graphic design, minimalist poster art, contemporary illustration.",
      "status": "not generated"
    },
    {
      "id": 7,
      "text": "Subject: Two actors, one dressed as Juliet, the other as Romeo, sharing a quiet moment of laughter or whispered lines off-stage, perhaps leaning against a prop tree or a sturdy park bench, just before their scene. Costumes are detailed with rich fabrics. Composition: Close-up to medium shot, focusing on their faces and the intimate connection between them, with a softly blurred park background. Lighting: Soft, diffused daylight filtering through dense leaves, creating dappled light and gentle highlights. Camera Angle: Slightly voyeuristic, eye-level, capturing an intimate, candid moment. Mood: Intimate, tender, authentic, anticipation. Style: Photojournalistic, backstage editorial, candid.",
      "status": "not generated"
    },
    {
      "id": 8,
      "text": "Subject: A dramatic tableau of three actors in rich period costumes on an outdoor stage, caught mid-soliloquy or intense conflict. One actor's face is powerfully half-lit, half-shadowed. Composition: Medium shot, strong triangular composition between the actors, emphasizing tension and drama. Lighting: High contrast lighting, a powerful spotlight from one side creating deep, theatrical shadows, with a subtle hint of the moon or distant city lights in the far background. Camera Angle: Slightly low, emphasizing the drama and power of the scene. Mood: Intense, dramatic, mysterious, powerful. Style: Film noir inspired, theatrical photography, Caravaggio-esque lighting.",
      "status": "not generated"
    },
    {
      "id": 9,
      "text": "Subject: A bustling park scene at dusk, with vibrant, glowing stage lights illuminating actors in motion (slightly blurred from a longer exposure). Around the stage, a diverse crowd of people is visible, some with picnic blankets, their blurred movement creating a sense of dynamic energy. Composition: Wide shot, capturing the entire scope of the event. The brightly lit stage acts as a focal point against the darkening sky. Lighting: Dusk transitioning to early night, strong artificial stage lighting contrasted with the fading natural light. Ambient fairy lights are visible in surrounding trees. Camera Angle: High angle, looking over the crowd towards the stage, capturing the scale and vibrancy of the gathering. Mood: Energetic, lively, communal, vibrant, magical night. Style: Long exposure photography, dynamic, slightly impressionistic.",
      "status": "not generated"
    },
    {
      "id": 10,
      "text": "Subject: A young adult (gender-neutral) sitting casually on a classic park bench, deeply engrossed in a worn copy of Shakespeare. Subtle, ethereal, glowing projections of famous Shakespearean characters (ghostly, transparent figures like Titania or Macbeth) appear in the trees behind them, interacting with the foliage. Composition: Medium shot, focused on the person and the book, with the ethereal characters subtly present in the background. Lighting: Late afternoon, dappled sunlight filtering through leaves, combined with an ethereal glow for the projected characters. Camera Angle: Eye-level, slightly candid, capturing a moment of quiet contemplation and imagination. Mood: Reflective, imaginative, inspiring, bridging past and present. Style: Magical realism, contemporary art, slightly surreal.",
      "status": "not generated"
    }
  ]
}

```
### Prompt to Image:

### Input
```text
"Subject: A diverse group of people (families, couples, friends) picnicking on blankets, laughing, and looking towards an outdoor stage in a lush park. Composition: Wide shot, capturing the expanse of the park, the audience in the foreground, and a glimpse of actors on a simple wooden stage in the mid-ground, framed by large oak trees. Lighting: Golden hour, warm and soft sunlight casting long, gentle shadows. Camera Angle: Slightly elevated, looking down gently on the audience, eye-level with the stage. Mood: Joyful, communal, relaxed, inviting, magical. Style: Editorial photography, naturalistic, slightly idealized."
```
### Output
<img src="sample output\images\ad-for-shakespeare-in-the-park-prompt-1-20260811_144119.png" alt="sample output image" width="500" align="center">
