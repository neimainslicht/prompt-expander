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
| `GEMINI_MAX_REQUESTS_PER_MINUTE` / `_PER_DAY` | `5` / `50` | Free-tier request caps. Conservative app-side values, not Google's published limits: check yours in AI Studio |
| `HF_MONTHLY_BUDGET_USD` | `4.75` | Application-side Hugging Face budget (USD) |
| `HF_BUDGET_SAFETY_MARGIN_PERCENT` | `10` | Share of that budget held back, so the app plans to spend at most 90% |

Gemini (free tier) and Hugging Face (prepaid credits) are configured separately: Gemini is limited by request counts and is not treated as spending, while Hugging Face has a dollar budget. The app's figures are its own estimates and will not exactly match your provider balance; the provider's dashboard is the source of truth.

**Status:** this stage only defines and validates the settings. The limits are enforced in later stages (validation, usage tracking, quotas and budgets), so the values above do not yet restrict generation.

### Credential safety
Keys are read only through `config.require_secret()` right before a provider is used. Error messages and logs go through `logging_config.redact()`, which removes the configured keys and key-shaped strings (Bearer tokens, `hf_...`, `AIza...`, `Authorization:`/`api_key=` values). Logging records exception tracebacks unredacted, so don't log exceptions with `exc_info`.

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
