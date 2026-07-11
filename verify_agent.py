"""Verification Agent: checks each resource's website against the database record.

For every resource with a website it fetches the page, asks Gemini to compare
the page against the stored record, and writes a verification_checks row.
Nothing is written to the resource itself except in one case: a high-confidence
"confirmed" with no proposed changes auto-updates last_verified_date (that is a
verification record, not a content change). Everything else waits in the
Review Queue for a human to approve or reject.

Resources without a website are marked needs_manual_check - we never pretend
to have verified something we could not see.

Usage:
    python verify_agent.py [--limit N] [--category "Food"] [--id 42]

Needs a Gemini API key. Provide it either way:
  - paste it into the file  gemini_api_key.txt  (one line), or
  - set the environment variable  GEMINI_API_KEY
"""
import argparse
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal

import certifi
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel

from db import get_conn, init_db

# Which Gemini model to use. Flash is the right tier for this comparison task -
# no need for anything pricier. Alternatives available on this account:
#   "gemini-3.5-flash"     - newer/stable, also fine
#   "gemini-flash-latest"  - alias that tracks the current Flash
# (gemini-2.5-flash is retired for new API keys as of mid-2026.)
MODEL = "gemini-3-flash-preview"

API_KEY_FILE = Path(__file__).parent / "gemini_api_key.txt"
_KEY_PLACEHOLDER = "PASTE_YOUR_GEMINI_API_KEY_HERE"

FETCH_TIMEOUT = 20
PAGE_TEXT_LIMIT = 20000  # generous: contact info / FAQs are often lower on the page
AUTO_APPLY_CONFIDENCE = 0.8

# Fields the agent may propose changes to. Anything else is ignored on apply.
PROPOSABLE_FIELDS = (
    "phone", "email", "location", "hours", "website", "notes",
    "bilingual_support", "accessibility", "insurance_cost",
    "eligibility_criteria", "seasonal_note", "status",
)

RECORD_FIELDS = (
    "org_name", "type", "category", "area", "phone", "email", "location",
    "hours", "website", "notes", "bilingual_support", "accessibility",
    "insurance_cost", "eligibility_criteria", "seasonal_note", "status",
)


# ---------- structured output schema (Gemini fills this) ----------

class ProposedChange(BaseModel):
    field: str        # one of PROPOSABLE_FIELDS; anything else is dropped on our side
    new_value: str


class VerificationResult(BaseModel):
    verdict: Literal["confirmed", "changed", "possibly_inactive"]
    confidence: float
    proposed_changes: list[ProposedChange]
    evidence: str


SYSTEM_PROMPT = """You are a verification agent for a pediatric clinic's community resource directory. Care coordinators use this directory to refer low-income families to assistance programs, so stale phone numbers or ended programs cause real harm.

You are given a database record for one resource and the text of that resource's website. Compare them and report:

- verdict "confirmed": the page is consistent with the record and the program appears to be operating and accepting applications.
- verdict "changed": the program is running but specific fields in the record are out of date (phone, hours, address, eligibility, cost, etc.). Propose the corrected values.
- verdict "possibly_inactive": the page suggests the program has ended, lost funding, closed its waitlist indefinitely, or the organization no longer offers it. Look for phrases like "funding is no longer available", "program ended", "waitlist closed", "no longer accepting applications". If you propose a status change, use "needs_review" rather than "inactive" - a human makes the final call.

Rules:
- Only propose a change when the page clearly supports it. Do not propose rewording, formatting tweaks, or additions the page does not state.
- proposed_changes.field must be one of exactly these: {fields}. Never invent a field name.

Contact information (phone, email) - be conservative, wrong numbers get families turned away:
- Scan the ENTIRE page before proposing any phone or email change. If the record's current phone or email still appears anywhere on the page, do NOT propose changing it.
- Only propose a phone/email change when the new value is clearly THE contact for this specific program or organization. A statewide/national hotline, a general "contact us" or main switchboard number, or a "find your local/county agency" locator is usually NOT the right per-program contact. If that is all the page offers, keep the existing phone and instead explain the situation in "evidence" (e.g. "page lists only the statewide LEAP hotline; the actual application contact is county-specific and requires their county locator - verify manually").
- If the real contact appears to sit behind an FAQ, an expandable section, or a separate "contact"/"apply"/"county" page that is NOT in the text you were given, say so in "evidence" and do not invent or guess a replacement number.
- When you do propose a contact change, your "evidence" must say where on the page that exact number/email appears and why it is the right one for this record.
- Seasonal programs (e.g. "applications open Nov 1", "winter shelter opens in October") are NOT inactive. Report them as confirmed or changed and propose a concise "seasonal_note" value describing the season/dates.
- The page may cover many programs; judge only the one in the record.
- "evidence" must quote or closely paraphrase the specific sentence(s) on the page that justify your verdict and each proposed change. If the page simply matches the record, say what you checked.
- confidence is 0 to 1: how sure you are of the verdict given what the page shows. If the page text is thin (mostly navigation, blocked content), lower your confidence.
- Today's date is {today}."""


# ---------- API key + client ----------

def load_api_key():
    """Return the Gemini API key from env or gemini_api_key.txt, or None."""
    key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if key:
        return key
    if API_KEY_FILE.exists():
        key = API_KEY_FILE.read_text(encoding="utf-8").strip()
        if key and key != _KEY_PLACEHOLDER:
            return key
    return None


def make_client():
    """Return a genai.Client, or None if no key is configured."""
    key = load_api_key()
    return genai.Client(api_key=key) if key else None


NO_KEY_MESSAGE = (
    "No Gemini API key configured - paste your key into gemini_api_key.txt "
    "(or set GEMINI_API_KEY) and run verification again."
)


# ---------- page fetching ----------

class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self.parts.append(data.strip())


def html_to_text(html):
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    text = "\n".join(parser.parts)
    return re.sub(r"\n{3,}", "\n\n", text)[:PAGE_TEXT_LIMIT]


# A domain like "samhousedenver.org" or "cuanschutz.edu". Used to salvage
# Website fields that hold a page *title* instead of a link.
_DOMAIN_RE = re.compile(
    r"\b((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
    r"(?:org|com|net|edu|gov|us|info|health))\b", re.I)


def normalize_url(url):
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def resolve_url(raw):
    """Turn a possibly-messy Website field into a fetchable URL, or '' if hopeless.

    Handles the common source-data problem where the field holds a page title
    such as "Samaritan House Denver | ... (samhousedenver.org)" - we pull the
    domain out rather than giving up. A clean URL is returned unchanged.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    # Already a proper single-token URL.
    if re.match(r"^https?://\S+$", raw):
        return raw
    # Has a protocol but trailing junk/spaces - keep the first token.
    m = re.match(r"^(https?://\S+)", raw)
    if m:
        return m.group(1)
    # Bare domain/path with no spaces, e.g. "energyoutreach.org/programs".
    if " " not in raw and "." in raw:
        return normalize_url(raw)
    # A title string: prefer a domain inside parentheses, else the first one.
    for chunk in re.findall(r"\(([^)]*)\)", raw):
        m = _DOMAIN_RE.search(chunk)
        if m:
            return "https://" + m.group(1).lower()
    m = _DOMAIN_RE.search(raw)
    if m:
        return "https://" + m.group(1).lower()
    return ""  # no usable link - caller records unreachable


def fetch_page(url):
    """Returns (text, final_url, http_status, error). text is None on failure."""
    url = resolve_url(url)
    if not url:
        return None, None, None, ("Website field is not a usable link (looks like "
                                  "a page title) - verify by phone.")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; CommunityResourceNavigator/1.0; "
                      "clinic resource verification)",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    tls = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT, context=tls) as resp:
            raw = resp.read(2_000_000)
            charset = resp.headers.get_content_charset() or "utf-8"
            html = raw.decode(charset, errors="replace")
            return html_to_text(html), resp.geturl(), resp.status, None
    except urllib.error.HTTPError as e:
        return None, url, e.code, f"HTTP {e.code} {e.reason}"
    except Exception as e:  # URLError, timeout, TLS, bad host
        return None, url, None, f"{type(e).__name__}: {e}"


# ---------- LLM comparison ----------

class RateLimitReached(Exception):
    """The Gemini quota is used up (e.g. free-tier 20/day). Signals run() to stop
    the whole batch cleanly instead of marking every remaining resource 'error'."""


# 5xx are transient load spikes - short backoff and retry.
_SERVER_CODES = {500, 502, 503, 504}
_MAX_ATTEMPTS = 4
# A 429 whose suggested wait is longer than this is a per-day quota wall, not a
# per-minute throttle - no point blocking the run on it.
_MAX_429_WAIT = 30


def _retry_delay_seconds(err):
    m = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", str(err))
    return float(m.group(1)) if m else None


def _generate_with_retry(client, **kwargs):
    for attempt in range(_MAX_ATTEMPTS):
        last = attempt == _MAX_ATTEMPTS - 1
        try:
            return client.models.generate_content(**kwargs)
        except genai_errors.APIError as e:
            code = getattr(e, "code", None)
            if code == 429:
                delay = _retry_delay_seconds(e)
                if last:
                    raise RateLimitReached(str(e))
                if delay is None:
                    time.sleep(2 ** attempt)          # unknown wait: brief backoff
                    continue
                if delay <= _MAX_429_WAIT:
                    time.sleep(delay + 1)              # per-minute throttle: wait it out
                    continue
                raise RateLimitReached(str(e))        # per-day quota exhausted: stop
            if code in _SERVER_CODES and not last:
                time.sleep(2 ** attempt)  # 1, 2, 4s
                continue
            raise


def compare_with_llm(client, record, page_text, source_url):
    record_json = json.dumps(
        {k: record[k] for k in RECORD_FIELDS}, indent=2, ensure_ascii=False)
    system_prompt = SYSTEM_PROMPT.format(
        today=date.today().isoformat(), fields=", ".join(PROPOSABLE_FIELDS))
    response = _generate_with_retry(
        client,
        model=MODEL,
        contents=(
            f"DATABASE RECORD:\n{record_json}\n\n"
            f"WEBSITE ({source_url}):\n{page_text}"
        ),
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0,
            response_mime_type="application/json",
            response_schema=VerificationResult,
        ),
    )
    result = json.loads(response.text)
    # list of {field, new_value} -> dict, whitelisted
    result["proposed_changes"] = {
        c["field"]: c["new_value"].strip()
        for c in result.get("proposed_changes", [])
        if c.get("field") in PROPOSABLE_FIELDS and c.get("new_value", "").strip()
    }
    return result


# ---------- check lifecycle ----------

def record_check(conn, resource_id, **fields):
    conn.execute(
        "UPDATE verification_checks SET review_status = 'superseded' "
        "WHERE resource_id = ? AND review_status = 'pending'", (resource_id,))
    fields.update(resource_id=resource_id,
                  checked_at=datetime.now().isoformat(timespec="seconds"))
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    conn.execute(f"INSERT INTO verification_checks ({cols}) VALUES ({marks})",
                 list(fields.values()))
    conn.commit()


def verify_resource(client, conn, resource, log=print):
    """Runs one verification and records the check. Returns the verdict string."""
    rid = resource["id"]
    website = (resource["website"] or "").strip()

    if not website:
        record_check(conn, rid, verdict="needs_manual_check",
                     evidence="No website on record - verify by phone.")
        return "needs_manual_check"

    page_text, final_url, http_status, fetch_error = fetch_page(website)
    if page_text is None or len(page_text.strip()) < 80:
        error = fetch_error or "page had no readable text (likely blocks scraping)"
        record_check(conn, rid, verdict="unreachable", source_url=final_url,
                     http_status=http_status, error=error,
                     evidence="Website could not be read. Unreachable does not "
                              "mean inactive - verify by phone.")
        return "unreachable"

    if client is None:
        record_check(conn, rid, verdict="error", source_url=final_url,
                     http_status=http_status, error=NO_KEY_MESSAGE)
        return "error"
    try:
        result = compare_with_llm(client, resource, page_text, final_url)
    except RateLimitReached:
        raise  # let run() stop the batch; don't record a bogus 'error' for this row
    except Exception as e:
        record_check(conn, rid, verdict="error", source_url=final_url,
                     http_status=http_status, error=f"{type(e).__name__}: {e}")
        return "error"

    verdict = result["verdict"]
    confidence = max(0.0, min(1.0, float(result["confidence"])))
    changes = result["proposed_changes"]

    auto_apply = (verdict == "confirmed" and not changes
                  and confidence >= AUTO_APPLY_CONFIDENCE)
    record_check(
        conn, rid,
        verdict=verdict,
        confidence=confidence,
        proposed_changes=json.dumps(changes, ensure_ascii=False) if changes else None,
        evidence=result["evidence"],
        source_url=final_url,
        http_status=http_status,
        review_status="auto_applied" if auto_apply else "pending",
    )
    if auto_apply:
        today = date.today().isoformat()
        conn.execute(
            "UPDATE resources SET last_verified_date = ?, verification_note = ?, "
            "updated_at = ? WHERE id = ?",
            (today, f"AI verified against website, no changes ({today})",
             datetime.now().isoformat(timespec="seconds"), rid))
        conn.commit()
    return verdict


def run(limit=None, category=None, resource_id=None, log=print):
    """Verify resources; yields a progress summary dict. Used by CLI and the web app."""
    init_db()
    client = make_client()
    if client is None:
        log("WARNING: " + NO_KEY_MESSAGE)
    conn = get_conn()
    sql = "SELECT * FROM resources WHERE status != 'inactive'"
    params = []
    if resource_id:
        sql += " AND id = ?"
        params.append(resource_id)
    else:
        # Skip resources already waiting in the review queue - re-checking them
        # wastes API calls (a real concern on a 20/day free tier).
        sql += (" AND id NOT IN (SELECT resource_id FROM verification_checks "
                "WHERE review_status = 'pending')")
        if category:
            sql += " AND category = ?"
            params.append(category)
    sql += " ORDER BY last_verified_date, id"  # stalest first
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    resources = conn.execute(sql, params).fetchall()

    summary = {"total": len(resources), "done": 0, "verdicts": {},
               "current": None, "stopped": None}
    for resource in resources:
        summary["current"] = resource["org_name"]
        yield summary
        try:
            verdict = verify_resource(client, conn, resource, log=log)
        except RateLimitReached as e:
            summary["stopped"] = "rate_limit"
            summary["current"] = None
            log("STOPPED: Gemini rate limit reached after "
                f"{summary['done']} checked - {str(e)[:160]}")
            break
        summary["done"] += 1
        summary["verdicts"][verdict] = summary["verdicts"].get(verdict, 0) + 1
        log(f"  [{summary['done']}/{summary['total']}] "
            f"{resource['org_name']}: {verdict}")
    summary["current"] = None
    conn.close()
    yield summary


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, help="verify at most N resources")
    ap.add_argument("--category", help="only this category (sheet name)")
    ap.add_argument("--id", type=int, help="only this resource id")
    args = ap.parse_args()

    summary = None
    for summary in run(limit=args.limit, category=args.category,
                       resource_id=args.id):
        pass
    print(f"\nDone: {summary['done']} checked - {summary['verdicts']}")


if __name__ == "__main__":
    main()
