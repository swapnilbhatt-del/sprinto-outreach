"""Step 6: pick the point of contact via Hunter Domain Search, with a permanent cache and a monthly quota."""
from __future__ import annotations

import json
import re
from datetime import date

import httpx

HUNTER_URL = "https://api.hunter.io/v2/domain-search"
COMPANY_URL = "https://api.hunter.io/v2/companies/find"
ENRICH_CREDITS = 0.0                # Hunter's account API showed 15 company lookups billed 0 credits (2026-11-17)
VERIFY_URL = "https://api.hunter.io/v2/email-verifier"
VERIFY_CREDITS = 0.5               # budgeted; Hunter's X-Credits-Used header is stored with each result


class QuotaExhausted(RuntimeError):
    pass


def _month(today: date | None = None) -> str:
    return (today or date.today()).strftime("%Y-%m")


def _call(client, domain: str, api_key: str, filters: dict) -> tuple[dict | None, str]:
    try:
        r = client.get(HUNTER_URL, params={"domain": domain, "api_key": api_key, "limit": 10, **filters})
    except httpx.HTTPError as e:
        return None, f"error:{type(e).__name__}"          # never echo the URL: it carries the key
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text[:500]}
    if r.status_code == 200:
        return body, "api"
    return None, f"error:HTTP {r.status_code}"


def _merge(first: dict, second: dict) -> dict:
    """Filtered emails first, then any new ones from the unfiltered search (deduped by address)."""
    seen = {e.get("value") for e in (first.get("data") or {}).get("emails") or []}
    extra = [e for e in (second.get("data") or {}).get("emails") or [] if e.get("value") not in seen]
    merged = dict(second)
    merged["data"] = dict(second.get("data") or {})
    merged["data"]["emails"] = list((first.get("data") or {}).get("emails") or []) + extra
    return merged


def domain_search(store, domain: str, api_key: str | None, quota: int,
                  http: httpx.Client | None = None, today: date | None = None,
                  filters: dict | None = None, usable=None) -> tuple[dict | None, str]:
    """Returns (hunter_json, how). how = cache | api | api_unfiltered | no_key | quota_exhausted | error:<...>.
    A domain in the cache is never searched again, whatever the cached result was.
    `filters` (e.g. department=it,executive) are tried first. If that result is not `usable` (default:
    it has emails; the pipeline passes "has a ranked, verified contact"), one unfiltered search follows,
    only if quota remains, and both results are merged. Hunter bills only searches that return results."""
    cached = store.hunter_cached(domain)
    if cached:
        return json.loads(cached["response_json"]), "cache"
    if not api_key:
        return None, "no_key"
    usable = usable or (lambda b: bool((b.get("data") or {}).get("emails")))
    month = _month(today)
    client = http or httpx.Client(timeout=20)
    if store.hunter_credits(month) + 1 > quota:
        return None, "quota_exhausted"
    store.hunter_increment(month)                  # counted before the call: a failed call may still bill
    body, how = _call(client, domain, api_key, filters or {})
    if body is None:
        return None, how
    body.setdefault("meta", {})["filters_used"] = [filters or {}]
    if filters and not usable(body) and store.hunter_credits(month) + 1 <= quota:
        store.hunter_increment(month)
        second, how2 = _call(client, domain, api_key, {})
        if second is not None:
            body = _merge(body, second)
            body.setdefault("meta", {})["filters_used"] = [filters, {}]
            how = "api_unfiltered"
    store.hunter_cache_put(domain, 200, body)
    return body, how


def _matches(position: str, aliases: list[str]) -> str | None:
    p = " " + re.sub(r"[^a-z ]", " ", position.lower()) + " "
    p = re.sub(r"\s+", " ", p)
    for a in aliases:
        if f" {a} " in p:
            return a
    return None


def name_order_warning(first: str, last: str | None, email: str) -> str | None:
    """Hunter sometimes swaps first and last names. If the address starts with the last name and
    doesn't contain the first name, say so: the greeting may use the wrong name. Never auto-corrected."""
    local = re.sub(r"[^a-z]", "", (email or "").split("@")[0].lower())
    f, l = re.sub(r"[^a-z]", "", (first or "").lower()), re.sub(r"[^a-z]", "", (last or "").lower())
    if f and l and local.startswith(l[:3]) and f[:3] not in local:
        return (f'check the name: Hunter gives first name "{first}", last name "{last}", but the address '
                f'starts with the last name, so they may be swapped')
    return None


def choose_contact(hunter: dict, ranking: list[str], aliases: dict[str, list[str]],
                   accepted_status: list[str]) -> tuple[dict | None, str, list[dict]]:
    """(contact, reason, considered). First title in `ranking` with a verified email wins; ties -> confidence."""
    emails = (hunter or {}).get("data", {}).get("emails", []) or []
    considered, best = [], None
    for e in emails:
        pos = e.get("position") or ""
        status = (e.get("verification") or {}).get("status")
        rank, title, alias = None, None, None
        for i, t in enumerate(ranking):
            alias = _matches(pos, aliases.get(t, [t.lower()]))
            if alias:
                rank, title = i, t
                break
        row = {"name": f"{e.get('first_name') or ''} {e.get('last_name') or ''}".strip(), "position": pos,
               "email": e.get("value"), "verification": status, "confidence": e.get("confidence"),
               "rank": rank, "matched_title": title}
        if rank is None:
            row["rejected"] = "title not in ranked list"
        elif status not in accepted_status:
            row["rejected"] = f"email verification status {status!r} not accepted"
        elif not e.get("first_name"):
            row["rejected"] = "no first name"
        considered.append(row)
        if "rejected" not in row and (best is None or (rank, -(row["confidence"] or 0)) <
                                      (best[0]["rank"], -(best[0]["confidence"] or 0))):
            best = (row, e, alias)
    if not best:
        return None, f"no ranked-title contact with an accepted email among {len(emails)} Hunter results", considered
    row, e, alias = best
    contact = {"first_name": e["first_name"], "last_name": e.get("last_name"), "email": e["value"],
               "position": row["position"], "matched_title": row["matched_title"]}
    warning = name_order_warning(e["first_name"], e.get("last_name"), e["value"])
    if warning:
        contact["name_warning"] = warning
    reason = (f"Rank {row['rank'] + 1} of {len(ranking)} on the title list: position \"{row['position']}\" "
              f"matched \"{row['matched_title']}\" (via \"{alias}\"); Hunter verification \"{row['verification']}\", "
              f"confidence {row['confidence']}. "
              + ("Top of the ranked list." if row["rank"] == 0 else "No higher-ranked title had an accepted email."))
    return contact, reason, considered


def _size(text: str) -> int:
    t = text.strip().upper().replace(",", "")
    return int(float(t[:-1]) * 1000) if t.endswith("K") else int(t)


def parse_employee_range(r: str | None) -> tuple[int, int] | None:
    """Hunter size buckets: "51-200", "251-1K", "1K-5K", "10K+". None if unparseable."""
    if not r:
        return None
    try:
        if r.strip().endswith("+"):
            return _size(r.strip()[:-1]), 10 ** 9
        lo, hi = r.split("-")
        return _size(lo), _size(hi)
    except ValueError:
        return None


def company_headcount(store, domain: str, api_key: str | None, quota: int,
                      http: httpx.Client | None = None, today: date | None = None) -> tuple[list[dict], str]:
    """Hunter Company Enrichment -> an employee_count fact. (facts, outcome).
    outcome: ok | cache | no_key | quota_exhausted | no_size | error:<...>. Each domain is looked up once, ever."""
    cached = store.hunter_company_cached(domain)
    if cached:
        body, outcome = json.loads(cached["response_json"]), "cache"
    else:
        if not api_key:
            return [], "no_key"
        month = _month(today)
        if store.hunter_credits(month) + ENRICH_CREDITS > quota:
            return [], "quota_exhausted"
        store.hunter_increment(f"{month}/enrich")      # counted before the call
        client = http or httpx.Client(timeout=20)
        try:
            r = client.get(COMPANY_URL, params={"domain": domain, "api_key": api_key})
        except httpx.HTTPError as e:
            return [], f"error:{type(e).__name__}"      # never echo the URL: it carries the key
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text[:500]}
        if r.status_code not in (200, 404):
            return [], f"error:HTTP {r.status_code}"
        store.hunter_company_put(domain, r.status_code, body)   # a 404 (unknown company) is cached too
        outcome = "ok"
    data = (body or {}).get("data") or {}
    m = data.get("metrics") or {}
    bucket, estimate = m.get("employees"), m.get("employeesCount")
    rng = parse_employee_range(bucket)
    indexed = (data.get("indexedAt") or "")[:10] or None
    if not rng or not indexed:
        return [], "no_size"
    quote = (f"Hunter company data for {domain}: employees {bucket}"
             + (f" (estimate {estimate})" if estimate else "") + f", indexed {indexed}")
    as_of = indexed
    day = (today or date.today()).isoformat()
    if indexed > day:          # a date in the future can't be trusted as "fresh": date it no later than today
        as_of = day
        quote += f" (a future date; treated as {day})"
    return [{"key": "employee_count", "value": {"value": bucket, "min": rng[0], "max": rng[1]},
             "quote": quote, "source_url": f"{COMPANY_URL}?domain={domain}", "source_type": "vendor",
             "date": as_of, "date_basis": "indexed"}], outcome


def verify_email(store, email: str, api_key: str | None, quota: int, http: httpx.Client | None = None,
                 today: date | None = None) -> tuple[str | None, str]:
    """Hunter Email Verifier -> (status, outcome). status: valid | invalid | accept_all | webmail |
    disposable | unknown | None. outcome: cache | api | no_key | quota_exhausted | pending | error:<...>.
    Each address is verified once, ever; the API key never appears in errors."""
    cached = store.hunter_verify_cached(email)
    if cached:
        return ((json.loads(cached["response_json"]).get("data") or {}).get("status"), "cache")
    if not api_key:
        return None, "no_key"
    month = _month(today)
    if store.hunter_credits(month) + VERIFY_CREDITS > quota:
        return None, "quota_exhausted"
    store.hunter_increment(f"{month}/verify")          # counted before the call
    client = http or httpx.Client(timeout=30)
    try:
        r = client.get(VERIFY_URL, params={"email": email, "api_key": api_key})
    except httpx.HTTPError as e:
        return None, f"error:{type(e).__name__}"
    if r.status_code == 202:
        return None, "pending"                         # Hunter is still checking; not cached, not retried here
    if r.status_code != 200:
        return None, f"error:HTTP {r.status_code}"
    try:
        body = r.json()
    except ValueError:
        return None, "error:unparseable"
    body.setdefault("meta", {})["credits_used_header"] = r.headers.get("X-Credits-Used")
    store.hunter_verify_put(email, r.status_code, body)
    return (body.get("data") or {}).get("status"), "api"


def verify_unverified_candidates(hunter: dict, ranking: list[str], aliases: dict[str, list[str]],
                                 verify, max_checks: int) -> tuple[dict, list[dict]]:
    """For contacts whose title is on the ranked list but whose email Hunter hasn't verified, run the
    Email Verifier on the best-ranked ones (at most `max_checks`). Returns (hunter body with updated
    statuses, log). `verify(email) -> (status, outcome)`."""
    body = json.loads(json.dumps(hunter or {}))
    rows = []
    for e in (body.get("data") or {}).get("emails") or []:
        status = (e.get("verification") or {}).get("status")
        rank = next((i for i, t in enumerate(ranking) if _matches(e.get("position") or "", aliases.get(t, [t.lower()]))), None)
        if rank is not None and status in (None, "", "unknown") and e.get("value") and e.get("first_name"):
            rows.append((rank, -(e.get("confidence") or 0), e))
    log = []
    for _, _, e in sorted(rows, key=lambda r: r[:2])[:max_checks]:
        status, outcome = verify(e["value"])
        log.append({"position": e.get("position"), "status": status, "outcome": outcome})
        if status:
            e["verification"] = {"status": status, "source": "hunter_email_verifier"}
    return body, log


ACCOUNT_URL = "https://api.hunter.io/v2/account"
FINDER_URL = "https://api.hunter.io/v2/email-finder"
FINDER_CREDITS = 1.0


def account_credits(api_key: str | None, http: httpx.Client | None = None) -> tuple[float | None, str | None]:
    """(credits remaining, reset date) from Hunter's account endpoint, which is free to call.
    (None, None) when there is no key or the call fails; the local counter then decides alone."""
    if not api_key:
        return None, None
    try:
        r = (http or httpx.Client(timeout=20)).get(ACCOUNT_URL, params={"api_key": api_key})
        d = (r.json() or {}).get("data") or {}
        return float(((d.get("requests") or {}).get("credits") or {}).get("remaining")), d.get("reset_date")
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return None, None


def effective_quota(store, configured: float, remaining: float | None, today: date | None = None) -> float:
    """The local cap, lowered to what Hunter says is really left, so the two never disagree in our favour."""
    if remaining is None:
        return configured
    return min(configured, store.hunter_credits(_month(today)) + remaining)


def find_email(store, domain: str, first: str, last: str, api_key: str | None, quota: float,
               http: httpx.Client | None = None, today: date | None = None) -> tuple[dict | None, str]:
    """Hunter Email Finder for a named person -> (data, outcome). Cached per (domain, name) forever.
    outcome: cache | api | no_key | quota_exhausted | not_found | error:<...>."""
    key = f"finder:{domain}:{first.lower()} {last.lower()}"
    cached = store.hunter_cached(key)
    if cached:
        data = json.loads(cached["response_json"]).get("data")
        return (data if data and data.get("email") else None), "cache"
    if not api_key:
        return None, "no_key"
    month = _month(today)
    if store.hunter_credits(month) + FINDER_CREDITS > quota:
        return None, "quota_exhausted"
    store.hunter_increment(f"{month}/finder")
    try:
        r = (http or httpx.Client(timeout=30)).get(FINDER_URL, params={
            "domain": domain, "first_name": first, "last_name": last, "api_key": api_key})
    except httpx.HTTPError as e:
        return None, f"error:{type(e).__name__}"
    try:
        body = r.json()
    except ValueError:
        return None, "error:unparseable"
    if r.status_code not in (200, 404):
        return None, f"error:HTTP {r.status_code}"
    store.hunter_cache_put(key, r.status_code, body)
    data = body.get("data")
    return (data, "api") if data and data.get("email") else (None, "not_found")
