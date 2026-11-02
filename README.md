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
| `GEMINI_MAX_REQUESTS_PER_MINUTE` / `_PER_DAY` | `5` / `50` | Free-tier request caps. Conservative app-side values, not Google's published limits: check yours in AI Studio |
| `HF_MONTHLY_BUDGET_USD` | `4.75` | Application-side Hugging Face budget (USD) |
| `HF_BUDGET_SAFETY_MARGIN_PERCENT` | `10` | Share of that budget held back, so the app plans to spend at most 90% |

Gemini (free tier) and Hugging Face (prepaid credits) are configured separately: Gemini is limited by request counts and is not treated as spending, while Hugging Face has a dollar budget. The app's figures are its own estimates and will not exactly match your provider balance; the provider's dashboard is the source of truth.

**Status:** request-size limits, the generation switch and usage tracking are in place (see below). Per-minute and daily limits and budgets are not enforced yet, so `MAX_GENERATION_REQUESTS_PER_MINUTE`, `MAX_IMAGES_PER_DAY`, the Gemini caps and the Hugging Face budget are defined but do not restrict anything yet.

**Hugging Face budget:** `HF_MONTHLY_BUDGET_USD` is the full amount you want to track, not a per-month allowance (the name will be changed when budgets are enforced). The 10% margin is applied once, in `Settings.hf_usable_budget_usd`. When budgets are enforced, spending is counted cumulatively across all months against that ceiling, so it does not reset monthly. If you add credits, raise the setting yourself.

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
- Cost columns exist but stay empty for now. Pricing is not guessed; cost estimates and budgets arrive with budget enforcement.
- Stored: ids, counts, timestamps, model names, token counts, error categories. Never stored: API keys, prompt text, raw provider error messages.
- Hugging Face spending will be counted cumulatively across all months, with no monthly reset, against `Settings.hf_usable_budget_usd`.

To look at recent usage:

```text
python -c "import usage; [print(r['created_at'], r['operation'], r['outcome'], r['items_succeeded'], '/', r['items_requested'], r['error_category']) for r in usage.get_store().list_requests(20)]"
```

Limits of this stage: a request stuck as `pending` after a crash is not cleaned up automatically, and calling `generatePrompts` / `generateImage` functions directly (not through `services`) is validated but not recorded.

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
