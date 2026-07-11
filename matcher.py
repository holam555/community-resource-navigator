"""Matching: anonymous family profile -> ranked resource list (PRD 功能C).

Two layers:
  1. Hard filter in SQL: active resources, requested categories, area matches
     (or the resource serves an unspecified / county-wide area).
  2. Gemini ranks the survivors against the anonymous profile and writes a
     one-sentence reason per match.

Privacy: the profile is never stored or logged. If Gemini is unavailable
(no key / free-tier quota exhausted), we fall back to the hard-filtered list
with generic reasons so staff still get something usable.
"""
import json
from datetime import date

from google.genai import types
from pydantic import BaseModel

from db import get_conn
from verify_agent import (MODEL, NO_KEY_MESSAGE, RateLimitReached,
                          _generate_with_retry, make_client)

MAX_CANDIDATES = 60   # cap what we send to Gemini
MAX_MATCHES = 12      # cap what staff see

# Compact view of each candidate for the model - enough to judge fit.
CANDIDATE_FIELDS = ("id", "org_name", "category", "type", "area",
                    "eligibility_criteria", "income_based", "bilingual_support",
                    "accessibility", "insurance_cost", "hours", "notes",
                    "seasonal_note")


class MatchItem(BaseModel):
    resource_id: int
    reason: str          # one sentence a coordinator can read aloud


class MatchResult(BaseModel):
    matches: list[MatchItem]   # best first
    note: str                  # overall guidance/caveats; empty if none


MATCH_PROMPT = """You are helping a pediatric clinic care coordinator match an anonymous family to community assistance resources. You get the family's anonymous profile and a list of candidate resources from the clinic's verified database.

Rules:
- Choose ONLY from the candidate list, referencing resources by their "id" as resource_id. Never invent a resource.
- Return up to {max_matches} matches, best fit first. A short, well-fitting list beats a padded one - leave out weak fits.
- Judge fit on: the family's area vs the resource's service area, eligibility criteria, income-based requirements, language needs (bilingual_support), the family's special circumstances, and seasonal availability. Today's date is {today}.
- If the free-text describes something with a strongly specific resource (for example medical equipment at home -> a utility medical exemption program; domestic violence -> DV advocates), rank that resource high and say why.
- "reason" is ONE sentence the coordinator can read aloud to the family: concrete, citing the fact that makes it a fit (e.g. "Serves Commerce City residents and has bilingual staff for energy-bill help"). Base it only on the candidate's own data - do not invent hours, staff, or services.
- "note": 1-2 sentences of overall guidance if needed (seasonal timing, documents to bring, categories with no good local option). Empty string if none.
- The profile is anonymous; never echo anything that could identify a person."""


def _candidates(conn, profile):
    sql = "SELECT * FROM resources WHERE status = 'active'"
    params = []
    needs = profile.get("needs") or []
    if needs:
        sql += f" AND category IN ({', '.join('?' for _ in needs)})"
        params += needs
    area = (profile.get("area") or "").strip()
    if area:
        # Keep resources that serve this area, an unspecified/statewide area,
        # or a county-wide area (the model can judge county fit better than SQL).
        sql += (" AND (area IS NULL OR area = 'Not Specified' "
                "OR area LIKE '%County%' OR instr(lower(area), lower(?)) > 0)")
        params.append(area)
    rows = [dict(r) for r in conn.execute(sql, params)]

    def sort_key(r):
        exact = area and r["area"] and area.lower() in r["area"].lower()
        return (0 if exact else 1, r["last_verified_date"] or "", r["org_name"])
    rows.sort(key=sort_key)
    return rows[:MAX_CANDIDATES]


def _clean_flag_text(value, yes="Yes"):
    """Excel-boolean noise -> family-readable text. 'False'/'No'/'0' -> None
    (hidden), 'True' -> 'Yes', real descriptions pass through."""
    if value is None:
        return None
    s = str(value).strip()
    if s.lower() in ("false", "no", "none", "n/a", "0", "-", ""):
        return None
    if s.lower() in ("true", "1"):
        return yes
    return s


def _decorate(row, reason):
    """Result item for the UI: resource fields + reason + staleness flag."""
    verified = row.get("last_verified_date")
    stale = (not verified) or (date.today() - date.fromisoformat(verified)).days > 90
    item = {**{k: row.get(k) for k in (
                "id", "org_name", "category", "type", "area", "phone", "email",
                "location", "hours", "website", "notes", "accessibility",
                "insurance_cost", "eligibility_criteria", "bilingual_support",
                "seasonal_note", "last_verified_date")},
            "reason": reason, "stale": stale}
    item["bilingual_support"] = _clean_flag_text(item["bilingual_support"])
    return item


def _basic_list(candidates, profile, mode, why=None):
    """Filter-only result. mode='basic' when the staffer chose it; 'fallback'
    when the AI path failed. No per-item reasons - the header explains enough,
    and repeating category/area on every card is noise."""
    rows = list(candidates)
    if profile.get("needs_spanish_or_bilingual"):
        # stable sort: bilingual-support resources first, area order preserved
        rows.sort(key=lambda r: 0 if _clean_flag_text(r.get("bilingual_support")) else 1)
    items = [_decorate(row, "") for row in rows[:MAX_MATCHES]]
    return {"mode": mode, "mode_reason": why, "note": "", "matches": items}


def find_matches(profile, use_ai=True):
    """profile: {needs: [category...], area: str, needs_spanish: bool,
                 circumstances: [str...], notes: str}. Never stored.
    use_ai=False skips Gemini entirely - instant, costs no API quota."""
    conn = get_conn()
    candidates = _candidates(conn, profile)
    conn.close()
    if not candidates:
        return {"mode": "basic" if not use_ai else "ai", "mode_reason": None, "note":
                "No active resources match those categories/area - try fewer filters.",
                "matches": []}

    if not use_ai:
        return _basic_list(candidates, profile, "basic")

    client = make_client()
    if client is None:
        return _basic_list(candidates, profile, "fallback", NO_KEY_MESSAGE)

    compact = [{k: r.get(k) for k in CANDIDATE_FIELDS if r.get(k) not in (None, "")}
               for r in candidates]
    try:
        response = _generate_with_retry(
            client,
            model=MODEL,
            contents=(
                "FAMILY PROFILE (anonymous):\n"
                + json.dumps(profile, ensure_ascii=False, indent=2)
                + "\n\nCANDIDATE RESOURCES:\n"
                + json.dumps(compact, ensure_ascii=False)
            ),
            config=types.GenerateContentConfig(
                system_instruction=MATCH_PROMPT.format(
                    max_matches=MAX_MATCHES, today=date.today().isoformat()),
                temperature=0,
                response_mime_type="application/json",
                response_schema=MatchResult,
            ),
        )
        result = json.loads(response.text)
    except RateLimitReached:
        return _basic_list(candidates, profile, "fallback",
                           "Gemini daily rate limit reached - showing the basic "
                           "filtered list instead. AI ranking returns when your "
                           "quota resets.")
    except Exception as e:
        return _basic_list(candidates, profile, "fallback",
                           f"AI ranking failed: {type(e).__name__}: {e}")

    by_id = {r["id"]: r for r in candidates}
    items = []
    for m in result.get("matches", [])[:MAX_MATCHES]:
        row = by_id.get(m.get("resource_id"))
        if row:  # ignore hallucinated ids
            items.append(_decorate(row, m.get("reason", "")))
    return {"mode": "ai", "mode_reason": None,
            "note": result.get("note", ""), "matches": items}
