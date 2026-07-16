# Community Resource Navigator - project rules

## Writing style (user-facing text)

- **Never use em dashes or en dashes** anywhere in user-facing text: UI copy,
  button labels, hints, error messages, printouts, share pages, emails.
  Rewrite with a period, comma, colon, or parentheses instead. Plain hyphens
  in compound words (e.g. "phone-friendly") are fine.
- This also applies to placeholder glyphs: use `&hellip;` for loading values
  and `&middot;` for empty table cells, not dashes.

## Quick pointers

- Stack, deploy steps, and env vars: see [README.md](README.md) and
  [DEPLOY.md](DEPLOY.md).
- Secrets live in `DEPLOY_SECRETS.local.md` (gitignored). Never commit it,
  `gemini_api_key.txt`, or `resources.db`. The repo is public.
- Database layer is dual-backend (SQLite locally, Postgres/Supabase in
  production): keep SQL portable per the rules in [db.py](db.py)'s docstring.
