# Deploying for the staff pilot (Render + Supabase, free tiers)

One-time setup, roughly 30–45 minutes of clicking. After this, nobody ever
runs a script: staff use the website, you deploy updates with `git push`.

The app runs on **Render** (free web service) and stores everything in
**Supabase** (free Postgres). Backups run weekly by themselves on GitHub.

---

## 1. Create the Supabase database (~5 min)

1. Go to https://supabase.com → **New project**. Pick any name
   (e.g. `resource-navigator`), a strong database password (save it), and the
   region closest to the clinic.
2. When it finishes, click **Connect** (top of the dashboard) and find the
   **Session pooler** connection string. It looks like:

   ```
   postgresql://postgres.abcdefgh:YOUR-PASSWORD@aws-0-us-west-1.pooler.supabase.com:5432/postgres
   ```

   **Use the Session pooler one (port 5432)** — not the "direct" connection.
   (The direct host is IPv6-only on the free tier; the pooler works from
   everywhere, including Render and GitHub.) Replace `YOUR-PASSWORD` with the
   database password from step 1. This whole string is your `DATABASE_URL`.

## 2. Copy your existing data up (one time, ~2 min)

From the project folder on your machine:

```bash
DATABASE_URL="postgresql://...the string from step 1..." \
  .venv/bin/python migrate_to_supabase.py
```

You should see `resources: 168 rows` etc. Your local `resources.db` is
untouched and keeps working for local development.

(Starting fresh instead? Run `DATABASE_URL="..." python import_xlsx.py your.xlsx`.)

## 3. Create the Render web service (~10 min)

1. Go to https://render.com → sign up with your GitHub account.
2. **New → Web Service** → pick the `community-resource-navigator` repo.
3. Settings:
   - **Instance type:** Free
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `gunicorn app:app --workers 1 --threads 8 --timeout 120`
4. **Environment variables** (Add Environment Variable, four of them):

   | Key | Value |
   |---|---|
   | `DATABASE_URL` | the Session-pooler string from step 1 |
   | `GEMINI_API_KEY` | your Gemini key (same one as in `gemini_api_key.txt`) |
   | `APP_PASSWORD` | the shared team password you choose — see README |
   | `SECRET_KEY` | any long random string (run `python3 -c "import secrets; print(secrets.token_hex(32))"`) |

5. **Create Web Service.** First deploy takes a few minutes. Your app is at
   `https://<name>.onrender.com` — open it, sign in with the team password,
   and check the Directory shows your resources.

From now on, every `git push` to `main` redeploys automatically.

## 4. Keep it awake (~3 min)

Free Render services nap after 15 idle minutes (first visit then takes ~1 min).
Fix: create a free monitor at https://uptimerobot.com that pings
`https://<name>.onrender.com/login` every 5 minutes. Done — no more napping,
and you get an email if the site ever goes down. (Ping `/login`, not `/` —
it answers without a session.)

## 5. Turn on weekly backups (~5 min)

In the GitHub repo → **Settings → Secrets and variables → Actions** →
**New repository secret**, add two:

| Secret | Value |
|---|---|
| `SUPABASE_DB_URL` | the same Session-pooler string |
| `BACKUP_PASSPHRASE` | a long random string — **keep a copy in your password manager**; backups can't be opened without it |

That's it — `.github/workflows/backup.yml` runs every Monday, dumps the
database, encrypts it, and stores it under the repo's **Actions → artifacts**
(rolling 90 days). Run it once now by hand: **Actions → Weekly database
backup → Run workflow**, and confirm a green run. The weekly connection also
keeps the free Supabase project from pausing for inactivity.

Restoring a backup (hopefully never):

```bash
openssl enc -d -aes-256-cbc -pbkdf2 -pass pass:YOUR-PASSPHRASE \
  -in backup.sql.gz.enc | gunzip > backup.sql
psql "$DATABASE_URL" < backup.sql
```

Belt-and-suspenders: any staffer can also click **Export Excel** in the app
now and then and drop the file in a shared drive.

## 6. Hand the URL to staff

Send them `https://<name>.onrender.com` + the team password. They sign in
once per browser and stay signed in for 30 days. Share links you text to
families (`.../l/...`) work without the password by design.

---

## Later: your own domain (with Cloudflare)

1. Buy the domain; add it to Cloudflare (free plan).
2. Render → your service → **Settings → Custom Domains** → add
   `resources.yourdomain.org`; Render shows a CNAME target.
3. In Cloudflare DNS, add that CNAME. Done — HTTPS is automatic, and share
   links now carry your domain.

## Later: self-hosting (if the clinic wants it on their own machine)

Nothing needs rewriting — the data stays in Supabase, so the web app can move
anywhere:

1. On any always-on machine: clone the repo, `pip install -r requirements.txt`,
   run the same gunicorn command with the same four env vars.
2. Expose it with a **Cloudflare Tunnel** (free): no port forwarding, works
   behind clinic firewalls, gives you HTTPS on your domain.
3. Turn off the Render service. Same database, same data, zero migration.
