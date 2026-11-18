"""Contact fallback: when Hunter's domain search has no usable person, find the company's named leaders
in free news coverage, then ask Hunter's Email Finder for that exact person.

Nothing is guessed: a leader counts only if a news item states their name and role in one verbatim
quote, and an email counts only if Hunter returns it with verification status `valid`."""
from __future__ import annotations

import re

from .llm import BudgetExceeded, CallBudget, JsonLLM, LLMError
from .poc_hunter import _matches
from .research.news import Throttle, news_digest, news_search
from .research.validate import norm

SYSTEM = """You read a numbered list of news items about ONE company and list its current senior leaders.

Rules:
- Only people the items say lead THIS company (name and domain given). Ignore people at other companies,
  customers, investors and quoted analysts.
- For each person: full name as written, their role at this company as written (e.g. "co-founder and CEO"),
  the item number, and a verbatim quote (max 200 chars) from that item that contains both the name and the role.
- If an item lists several founders in one sentence, one quote may cover each of them.
- No outside knowledge. If no item names a leader, return an empty list."""

SCHEMA = {
    "type": "object",
    "properties": {"leaders": {"type": "array", "items": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "role": {"type": "string"},
                       "item": {"type": "string"}, "quote": {"type": "string"}},
        "required": ["name", "role", "item", "quote"], "additionalProperties": False}}},
    "required": ["leaders"], "additionalProperties": False,
}

QUERIES = ['"{company}" founder', '"{company}" CEO']


def split_name(full: str) -> tuple[str, str] | None:
    parts = [p for p in re.split(r"\s+", full.strip()) if p]
    if len(parts) < 2:
        return None
    return parts[0], parts[-1]


def find_leaders(fetcher, llm: JsonLLM, budget: CallBudget, company: str, domain: str, throttle,
                 ranking: list[str], aliases: dict[str, list[str]], per_query: int = 8) -> tuple[list[dict], dict]:
    """(leaders ranked by the contact title list, log). Each leader: name, role, matched_title, rank,
    quote, source_url. Free news only; at most one LLM call."""
    throttle = throttle or Throttle(0)
    results, outcomes = {}, {}
    for q in QUERIES:
        items, outcome = news_search(fetcher, q.format(company=company), per_query, throttle)
        outcomes[q] = outcome
        results[q] = items
    source = news_digest(results)
    log = {"queries": outcomes, "items": len(source.items)}
    if not source.items:
        return [], log
    if budget.remaining < 2:                         # keep a call for the email draft
        log["skipped"] = "llm_budget"
        return [], log
    try:
        raw = llm.call_json("leaders", SYSTEM, f"Company: {company} ({domain})\n\n<items>\n{source.render()}\n</items>",
                            SCHEMA, budget, max_tokens=2000)
    except (LLMError, BudgetExceeded) as e:
        log["error"] = str(e)
        return [], log
    kept, dropped = [], []
    for p in raw.get("leaders", []):
        item = source.items.get(str(p.get("item", "")).strip())
        quote, name = p.get("quote", ""), p.get("name", "")
        why = None
        if item is None:
            why = "item not in results"
        elif not quote.strip() or norm(quote) not in norm(item.text):
            why = "quote not found in item"
        elif norm(name) not in norm(quote):
            why = "name not in quote"
        elif not split_name(name):
            why = "need first and last name"
        if why:
            dropped.append({"name": name, "reason": why})
            continue
        role = p.get("role", "")
        rank = next((i for i, t in enumerate(ranking) if _matches(role, aliases.get(t, [t.lower()]))), None)
        if rank is None and re.search(r"\bfounder\b", role, re.I) and "Co-founder" in ranking:
            rank = ranking.index("Co-founder")     # "founded by X" in a dated news item names a founder
        if rank is None:
            dropped.append({"name": name, "reason": f"role {p.get('role')!r} not on the title list"})
            continue
        kept.append({"name": name.strip(), "role": p["role"].strip(), "matched_title": ranking[rank], "rank": rank,
                     "quote": quote.strip(), "source_url": item.url})
    kept.sort(key=lambda x: x["rank"])
    log.update(kept=len(kept), dropped=dropped)
    return kept, log
