"""Target-account discovery: recent funding news -> companies that raised -> their domains.

Free sources only (Bing News RSS, Clearbit's public name lookup) plus one LLM call to read the
headlines. A company is kept only when a headline or summary names it as the one that raised money,
quoted verbatim, and a domain is found whose name matches it exactly. Qualification happens later,
in the normal pipeline; discovery only proposes candidates."""
from __future__ import annotations

import json
import re
from urllib.parse import quote_plus, urlparse

import httpx

from .llm import BudgetExceeded, CallBudget, JsonLLM, LLMError
from .research.news import Throttle, news_digest, news_search
from .research.validate import norm
from .shortlist import normalise

CLEARBIT = "https://autocomplete.clearbit.com/v1/companies/suggest?query={q}"

SYSTEM = """You read numbered news items about startup funding and list the companies that RAISED money.

Rules:
- Only the company receiving the investment, never the investors, acquirers or customers.
- Only companies that sell software, a platform or an API to businesses (B2B). Skip consumer apps,
  hardware-only firms, lenders and funds.
- For each: the company name exactly as written, the round as written (e.g. "Series B"), the item
  number, and a verbatim quote (max 200 chars) from that item that contains the company name and
  shows the raise.
- No outside knowledge. Return an empty list if no item qualifies."""

SCHEMA = {
    "type": "object",
    "properties": {"companies": {"type": "array", "items": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "round": {"type": "string"},
                       "item": {"type": "string"}, "quote": {"type": "string"}},
        "required": ["name", "round", "item", "quote"], "additionalProperties": False}}},
    "required": ["companies"], "additionalProperties": False,
}


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def match_domain(name: str, suggestions: list[dict]) -> tuple[str | None, str]:
    """(domain, confidence). "exact": the domain itself is the company name (acme.com for Acme).
    "name_only": Clearbit lists a company of that name, but its domain is something else
    (Dreamteam -> dreamteampayrolloutsource.co.th), so a person should check it first."""
    k = _key(name)
    doms = [(normalise(s.get("domain") or ""), s) for s in suggestions or []]
    for dom, _ in doms:
        if dom and _key(dom.split(".")[0]) == k:
            return dom, "exact"
    for dom, s in doms:
        if dom and _key(s.get("name")) == k:
            return dom, "name_only"
    return None, "none"


def lookup_domain(name: str, user_agent: str, http: httpx.Client | None = None) -> tuple[str | None, str]:
    """(domain, how). Follows the homepage redirect once so fleetx.io becomes fleetx.ai."""
    client = http or httpx.Client(timeout=15, headers={"User-Agent": user_agent}, follow_redirects=True)
    try:
        r = client.get(CLEARBIT.format(q=quote_plus(name)))
        dom, conf = match_domain(name, r.json() if r.status_code == 200 else [])
    except (httpx.HTTPError, ValueError):
        return None, "lookup_error"
    if not dom:
        return None, "no_match"
    try:
        final = normalise(urlparse(str(client.head(f"https://{dom}/").url)).netloc)
        if final and final != dom and _key(final.split(".")[0]) == _key(dom.split(".")[0]):
            return final, f"{conf}, redirects to {final}"
    except httpx.HTTPError:
        pass
    return dom, conf


def discover(fetcher, llm: JsonLLM, seller: dict, user_agent: str, exclude: dict[str, str],
             researched: set[str], throttle: Throttle | None = None, http=None,
             progress=lambda msg: None) -> dict:
    """Returns {candidates: [...], dropped: [...], queries: {...}}. Each candidate: name, domain,
    round, quote, source_url, date, domain_how, status (new | check_domain | researched | excluded | no_domain)."""
    cfg = seller["discovery"]
    throttle = throttle or Throttle(2)
    results, outcomes = {}, {}
    for q in cfg["queries"]:
        progress(f"Searching news: {q}")
        items, outcome = news_search(fetcher, q, cfg.get("items_per_query", 10), throttle)
        results[q], outcomes[q] = items, outcome
    source = news_digest(results)
    out = {"queries": outcomes, "items": len(source.items), "candidates": [], "dropped": []}
    if not source.items:
        return out
    progress(f"Reading {len(source.items)} headlines")
    try:
        raw = llm.call_json("discover", SYSTEM, f"<items>\n{source.render()}\n</items>", SCHEMA,
                            CallBudget(2), max_tokens=4000)
    except (LLMError, BudgetExceeded) as e:
        out["error"] = str(e)
        return out
    seen = set()
    for c in raw.get("companies", []):
        item = source.items.get(str(c.get("item", "")).strip())
        name, quote = (c.get("name") or "").strip(), c.get("quote") or ""
        if item is None or not quote.strip() or norm(quote) not in norm(item.text) or norm(name) not in norm(quote):
            out["dropped"].append({"name": name, "reason": "quote not found in the news item"})
            continue
        if _key(name) in seen or not _key(name):
            continue
        seen.add(_key(name))
        out["candidates"].append({"name": name, "round": c.get("round", ""), "quote": quote.strip(),
                                  "source_url": item.url, "date": item.date})
        if len(out["candidates"]) >= cfg.get("max_candidates", 25):
            break
    for cand in out["candidates"]:
        progress(f"Finding the website for {cand['name']}")
        dom, how = lookup_domain(cand["name"], user_agent, http=http)
        cand["domain"], cand["domain_how"] = dom, how
        if not dom:
            cand["status"] = "no_domain"
        elif not how.startswith("exact"):
            cand["status"] = "check_domain"
        elif dom in exclude:
            cand["status"], cand["excluded_by"] = "excluded", exclude[dom]
        elif dom in researched:
            cand["status"] = "researched"
        else:
            cand["status"] = "new"
    return out


if __name__ == "__main__":                       # quick manual check: python -m src.discover
    from .config import load_env, load_icp
    from .llm import make_llm
    from .research.fetch import Fetcher
    load_env()
    icp = load_icp()
    res = discover(Fetcher(icp["agent_loop"]["user_agent"]), make_llm(icp["llm"]), icp["seller"],
                   icp["agent_loop"]["user_agent"], {}, set(), progress=print)
    print(json.dumps(res["candidates"], indent=1)[:4000])
