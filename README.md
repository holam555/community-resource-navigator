# Community Resource Navigator

Internal tool for clinic care coordinators: a searchable, editable database of
community resources (utilities, housing, food, legal, etc.) for low-income
families. It verifies each resource's website with AI (staff approve every
change) and matches families to the help they qualify for — anonymously.

**Status: Phase 3 complete** (P1: xlsx import, normalized schema,
searchable/editable list UI, Excel export. P2: AI verification agent + review
queue. P3: family→resource matching with AI ranking, copy button, and a
printable family handout). Phase 4 = scheduled runs + Maps export.

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
python import_xlsx.py "your-resources.xlsx"   # first run only: seed the database
.venv/bin/python app.py                        # http://localhost:5057
```

Then paste a Gemini API key into `gemini_api_key.txt` for verification/AI match
(see below). The resource spreadsheet is **not** in this repo — supply your own.

## Database & hosting

The app uses **SQLite** (`resources.db`, one file, no separate service). It is
**read-write**: staff edits, verification results, approvals, and shared match
links are all written back to it. That shapes where you can host it.

- **Recommended for the pilot — a host that runs a normal always-on process
  with a persistent disk:** Render, Railway, or Fly.io. SQLite works unchanged.
  Start command is in the `Procfile` (`gunicorn app:app`). Add a persistent
  disk mounted where `resources.db` lives so data survives redeploys, set the
  `GEMINI_API_KEY` env var, and seed once with `import_xlsx.py`.
- **Vercel is not a good fit as-is.** Vercel runs Python as *serverless
  functions* with a disposable, read-only filesystem and a short time limit, so
  (a) every edit/approval/verification result would be lost, and (b) the
  minutes-long verification run (a background thread) can't run there. To use
  Vercel you'd swap SQLite for a hosted database (Turso is SQLite-compatible and
  has a free tier; or Postgres/Neon) and move verification to a scheduled job —
  doable, but real work. For a first staff pilot, use one of the hosts above.

**Config via environment variables:** `GEMINI_API_KEY` (instead of the file),
`PORT` (host sets this), `FLASK_DEBUG=1` (local only — never in production).

## Verification (Phase 2)

The **Review Queue** tab checks each resource's website against the database
record using **Google Gemini 3 Flash** (`gemini-3-flash-preview`, the `MODEL`
constant in `verify_agent.py`; `gemini-3.5-flash` also works). Flash is the
right tier here — the job is text comparison + structured extraction, not
something that needs a premium model. Transient `503`/`429` responses from
Google are retried automatically.

**Paste your Gemini API key** into the file `gemini_api_key.txt` (one line,
replacing the placeholder), or set the `GEMINI_API_KEY` environment variable.
That file is gitignored so the key is never committed. Get a key from
https://aistudio.google.com/apikey.

Run verification from the UI button or the CLI:

```bash
.venv/bin/python verify_agent.py --limit 20          # stalest resources first
.venv/bin/python verify_agent.py --category "Food"   # one category
```

### Free-tier rate limits

Gemini's **free tier caps `gemini-3-flash-preview` at ~20 requests/day**, and
only fetchable resources use a request. The agent is built for this:

- **Skips resources already in the review queue**, so a daily run never wastes
  a request re-checking something you haven't reviewed yet.
- **Stalest-first**, so each day's run advances to the next un-checked batch.
- **Stops cleanly when the daily quota is hit** — verified items stay in the
  queue and the status line says "Paused at your Gemini rate limit"; it does
  *not* fill the queue with error cards. Run again after the quota resets.
- Per-minute throttles (`429` with a short `retryDelay`) are waited out and
  retried automatically.

So on the free tier you clear the ~127 fetchable resources over about a week of
daily clicks. **To do it in one go, enable billing on your Google AI Studio
project** — a full run is on the order of pennies on Flash — or switch `MODEL`
to a lighter model (e.g. `gemini-2.5-flash-lite`) whose free daily quota is
higher.

**Fetch coverage:** about 76% of the 168 resources have a website we can
fetch and hand to Gemini. (`verify_agent.resolve_url` salvages the ~7% whose
Website column holds a page *title* with a domain in it, e.g.
"... (samhousedenver.org)".) The rest can't be auto-checked no matter the model
— ~8% have no website, and the remainder are 404/403/SSL/JS-only pages or
title-only strings with no domain. All are marked `unreachable` or
`needs_manual_check` (never "inactive") and sort to the bottom of the queue so
staff know to verify by phone.

Design rules (see PRD §功能B):

- **AI only suggests; staff approve.** Every proposed change sits in the
  Review Queue with old → new values and a quoted page excerpt as evidence.
  Approve writes it to the database and stamps `last_verified_date`; Reject
  keeps the current values.
- One exception: a high-confidence **confirmed** with *no* proposed changes
  auto-updates `last_verified_date` only (logged as `auto_applied`) — that's a
  verification record, not a content change, and it keeps the monthly queue
  small enough to clear in minutes.
- No website → `needs_manual_check`. Unreachable website → `unreachable`
  (explicitly **not** the same as inactive — the UI says to verify by phone).
- Seasonal wording ("applications open Nov 1") is captured into
  `seasonal_note`, not treated as inactive.

## Matching (Phase 3)

The **Find Resources** tab takes an anonymous family profile — needs
(categories), city, language, situation checkboxes, optional free text — and
returns a matched list. **Two modes:**

- **Quick search** — instant, filter-only (category + city + language boost),
  uses **no AI quota**. Best area fits first.
- **AI match** — one Gemini call ranks the filtered candidates against the
  full situation and notes, with a one-sentence "why this fits" per resource.
  Costs one request; can take up to a minute on the free tier. If the key is
  missing or the daily quota is spent it falls back to the quick-search list
  with a visible notice instead of failing.

Layer 1 in both modes is the same hard SQL filter (active + category +
area/"Not Specified"/county-wide).
- Results are **grouped by category**, each card keeping its overall AI rank.
  Cards (and the printout) list eligibility, language support, how to apply,
  cost, and the resource's notes/benefit description — empty fields are
  skipped. Staff edit those descriptions per-resource in the Directory drawer.
- **Privacy:** the profile is never stored or logged; the form says so.
- **Four ways out:** Copy list (plain text), Print handout (print CSS strips
  the form/inputs — resources only), **Email list** (opens the staff mail app
  with the share link prefilled), and **Get share link** — saves the list
  (resource info + reasons only, rebuilt from the DB) under
  `/l/<random-token>` as a phone-friendly page staff can paste into a text
  message. Note: share/email links carry this server's address, so they only
  work outside the building once the app is hosted somewhere reachable.
- Matches show the verification badge; anything not verified within 90 days
  carries a "call to confirm" warning, per the PRD.

## Files

- `app.py` — Flask app: list/search/filter API, create/update, review-queue +
  verify endpoints, `/export.xlsx`
- `verify_agent.py` — the verification agent: fetch → readable-text extraction
  → Gemini comparison (Pydantic response schema, forced JSON) →
  `verification_checks` row. Reads the key from `gemini_api_key.txt` / env.
- `matcher.py` — Phase 3 matching: SQL hard filter + one Gemini ranking call,
  with a no-AI fallback. Reuses the client/retry logic from `verify_agent.py`.
- `db.py` — SQLite schema (`resources.db`): `resources` + `verification_checks`
- `import_xlsx.py` — one-time import from the original Excel; normalizes the
  per-sheet header variants and area-name typos. Refuses to run if the DB
  already has data (delete `resources.db` to re-import — edits will be lost).
- `templates/index.html` — the whole app UI (vanilla JS, no build step):
  landing page + Directory + Find Resources + Review Queue behind a tabbed
  topbar. `templates/share.html` is the phone-friendly shared-list page.

## Notes

- No patient data is stored anywhere in this tool, by design (see PRD §5).
- `last_verified_date` on imported rows is 2025-07-01 (the workbook was
  verified July 2025); rows older than 90 days show an amber warning in the UI.
