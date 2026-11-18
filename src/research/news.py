"""Free sources: Bing News RSS, public job-board APIs (Greenhouse/Lever/Ashby) and Wikidata."""
from __future__ import annotations

import html
import json
import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse

from .fetch import Fetcher
from .validate import Source, SourceItem

# Google News RSS (the brief's first choice) is disallowed by news.google.com/robots.txt, and GDELT
# rate-limited this network almost every time. Bing's news RSS is allowed by robots.txt, needs no key,
# and covers years of coverage. Its links are redirects carrying the publisher URL in `url=`.
BING_NEWS = "https://www.bing.com/news/search"


class Throttle:
    """Enforces a minimum gap between calls to one host, shared across companies in a run."""
    def __init__(self, min_interval: float, sleep=time.sleep, clock=time.monotonic):
        self.min_interval, self.sleep, self.clock = min_interval, sleep, clock
        self._last = None

    def wait(self) -> None:
        if self._last is not None:
            gap = self.min_interval - (self.clock() - self._last)
            if gap > 0:
                self.sleep(gap)
        self._last = self.clock()


def company_name(domain: str) -> str:
    return domain.split(".")[0]


CLEARBIT_SUGGEST = "https://autocomplete.clearbit.com/v1/companies/suggest?query={q}"


def resolve_company_name(fetcher: Fetcher, domain: str, override: str | None,
                         take_api) -> tuple[str, str]:
    """(name, how). Order: name given in the input file, then Clearbit autocomplete (only an
    exact domain match is accepted), then the domain stem. News queries and prompts use this name."""
    if override and override.strip():
        return override.strip(), "input_file"
    if take_api():
        r = fetcher.get(CLEARBIT_SUGGEST.format(q=quote_plus(company_name(domain))))
        if r.ok:
            try:
                for c in json.loads(r.text):
                    if (c.get("domain") or "").lower().removeprefix("www.") == domain and c.get("name"):
                        return c["name"].strip(), "clearbit"
            except (json.JSONDecodeError, AttributeError, TypeError):
                pass
    return company_name(domain), "domain_stem"


def publisher_url(link: str) -> str:
    """Bing wraps links as bing.com/news/apiclick.aspx?...&url=<publisher>. Return the publisher URL."""
    if "bing.com" in urlparse(link).netloc:
        target = parse_qs(urlparse(link).query).get("url", [""])[0]
        if target.startswith(("http://", "https://")):
            return target
    return link


def news_search(fetcher: Fetcher, query: str, limit: int, throttle: Throttle) -> tuple[list[SourceItem], str]:
    """Returns (items, outcome). Item text is 'title — publisher — date — summary'.
    outcome: ok | rate_limited | error | <fetch outcome>. Only `ok` means the source was checked."""
    url = BING_NEWS + "?" + urlencode({"q": query, "format": "rss", "mkt": "en-US"})
    for _ in range(2):                            # one retry on HTTP 429
        throttle.wait()
        r = fetcher.get(url)
        if r.status != 429:
            break
    else:
        return [], "rate_limited"
    if not r.ok:
        return [], r.outcome
    try:
        root = ET.fromstring(r.text)
    except ET.ParseError:
        return [], "error"
    items = []
    for it in root.iter("item"):
        title = html.unescape((it.findtext("title") or "").strip())
        link = publisher_url((it.findtext("link") or "").strip())
        summary = html.unescape(re.sub(r"<[^>]+>", " ", it.findtext("description") or "")).strip()[:300]
        publisher = next((ch.text or "" for ch in it if ch.tag.endswith("Source")), "").strip()
        try:
            d = parsedate_to_datetime(it.findtext("pubDate")).date().isoformat()
        except (TypeError, ValueError):
            d = None
        if title and link:
            items.append(SourceItem(url=link, source_type="news", date=d,
                                    text=f"{title} — {publisher} — {d or 'undated'} — {summary}"))
        if len(items) >= limit:
            break
    return items, "ok"


def news_digest(results: dict[str, list[SourceItem]]) -> Source:
    """Merge several queries' items into one numbered digest (one extract call instead of many)."""
    items, seen = {}, set()
    for qitems in results.values():
        for it in qitems:
            if it.url in seen:
                continue
            seen.add(it.url)
            items[str(len(items) + 1)] = it
    return Source(label="bing news digest", items=items)


def job_board_lookup(fetcher: Fetcher, domain: str, templates: list[str], keywords: list[str],
                     page_budget) -> tuple[list[dict], list[dict]]:
    """Deterministic, no LLM. Returns (facts, attempts). A board counts as found if the API answers 200."""
    slug = company_name(domain)
    kw = re.compile("|".join(re.escape(k) for k in keywords), re.I)
    facts, attempts = [], []
    for tpl in templates:
        if not page_budget():
            attempts.append({"url": tpl.format(slug=slug), "outcome": "skipped_cap"})
            continue
        url = tpl.format(slug=slug)
        r = fetcher.get(url)
        attempts.append({"url": url, "outcome": r.outcome, "status": r.status})
        if not r.ok:
            continue
        try:
            data = json.loads(r.text)
        except json.JSONDecodeError:
            attempts[-1]["outcome"] = "unparseable"
            continue
        jobs = data.get("jobs") if isinstance(data, dict) else data
        if not isinstance(jobs, list):
            attempts[-1]["outcome"] = "unparseable"
            continue
        for j in jobs:
            if not isinstance(j, dict):
                continue
            title = j.get("title") or j.get("text") or ""
            link = j.get("absolute_url") or j.get("hostedUrl") or j.get("jobUrl") or url
            if title and kw.search(title):
                facts.append({"key": "compliance_job_titles", "value": {"value": title}, "quote": title,
                              "source_url": link, "source_type": "job_board", "date": None,
                              "date_basis": "live_posting"})
        attempts[-1]["jobs_seen"] = len(jobs or [])
        break                          # first board that answers is the company's board
    return facts, attempts


def wikidata_employees(fetcher: Fetcher, domain: str) -> tuple[list[dict], str]:
    """Find the Wikidata item whose official website (P856) is this domain; read employees (P1128)."""
    search = ("https://www.wikidata.org/w/api.php?action=wbsearchentities&format=json&language=en&limit=5"
              f"&search={quote_plus(company_name(domain))}")
    r = fetcher.get(search)
    if not r.ok:
        return [], r.outcome
    try:
        ids = [e["id"] for e in json.loads(r.text).get("search", [])]
    except (json.JSONDecodeError, AttributeError, KeyError, TypeError):
        return [], "error"
    if not ids:
        return [], "no_match"
    r = fetcher.get("https://www.wikidata.org/w/api.php?action=wbgetentities&format=json&props=claims"
                    f"&ids={'|'.join(ids)}")
    if not r.ok:
        return [], r.outcome
    try:
        entities = json.loads(r.text).get("entities", {})
    except (json.JSONDecodeError, AttributeError):
        return [], "error"
    for qid, ent in entities.items():
        claims = ent.get("claims", {})
        sites = [c["mainsnak"].get("datavalue", {}).get("value", "") for c in claims.get("P856", [])]
        if not any(re.search(rf"//(www\.)?{re.escape(domain)}(/|$)", s) for s in sites):
            continue
        best = None
        for c in claims.get("P1128", []):
            amount = c["mainsnak"].get("datavalue", {}).get("value", {}).get("amount")
            when = (c.get("qualifiers", {}).get("P585") or [{}])[0].get("datavalue", {}).get("value", {}).get("time")
            if when:                   # "+2023-00-00T..." -> earliest date the claim could mean
                y, m, d = when[1:11].split("-")
                when = f"{y}-{m if m != '00' else '01'}-{d if d != '00' else '01'}"
            if amount and (best is None or (when or "") > (best[1] or "")):
                best = (int(float(amount)), when)
        if best:
            n, when = best
            quote = f"Wikidata {qid} P1128 employees: {n}" + (f" (point in time {when})" if when else "")
            return [{"key": "employee_count", "value": {"value": str(n), "min": n, "max": n}, "quote": quote,
                     "source_url": f"https://www.wikidata.org/wiki/{qid}", "source_type": "wikidata",
                     "date": when, "date_basis": "stated"}], "ok"
        return [], "no_employee_claim"
    return [], "no_match"
