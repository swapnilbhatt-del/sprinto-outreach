"""Schema check + quote check. A fact survives only if its quote appears in the source text."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .extract import SCHEMA

SIMPLE_KEYS = ["is_software_product", "sells_to_businesses", "enterprise_motion",
               "regulated_vertical", "eu_presence"]


@dataclass
class SourceItem:
    """One quotable unit: a page, or one news item / job posting / Wikidata claim."""
    url: str
    text: str
    source_type: str                 # page | news | job_board | wikidata
    date: str | None = None          # publication date (news) or fetch date (page), YYYY-MM-DD


@dataclass
class Source:
    label: str
    items: dict[str, SourceItem]     # "none" for a single page, "1".."n" for a news digest
    fetched_on: str = field(default_factory=lambda: date.today().isoformat())

    def render(self) -> str:
        if list(self.items) == ["none"]:
            return self.items["none"].text
        return "\n\n".join(f"[{k}] {it.text}" for k, it in self.items.items())


def norm(s: str) -> str:
    s = s.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace(" ", " ")
    s = re.sub(r"[\"'`*_]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_date(s: str | None) -> str | None:
    if not s:
        return None
    m = re.match(r"^(\d{4})-(\d{2})(?:-(\d{2}))?$", s.strip())
    if not m:
        return None
    y, mo, d = int(m[1]), int(m[2]), int(m[3] or 1)
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def schema_errors(raw: dict) -> list[str]:
    errs = []
    for key, spec in SCHEMA["properties"].items():
        if key not in raw:
            errs.append(f"missing {key}")
            continue
        if spec.get("type") == "object":
            enum = spec["properties"]["value"].get("enum")
            if enum and raw[key].get("value") not in enum:
                errs.append(f"{key}.value not in enum")
        elif not isinstance(raw[key], list):
            errs.append(f"{key} must be a list")
    return errs


def _int(s) -> int | None:
    s = re.sub(r"[,\s+]", "", str(s or ""))
    return int(s) if s.isdigit() else None


ROUND_WORDS = {
    "pre_seed": r"pre[- ]?seed", "seed": r"\bseed\b",
    "series_a": r"series[- ]?a\b", "series_b": r"series[- ]?b\b", "series_c": r"series[- ]?c\b",
    "series_d_plus": r"series[- ]?[d-k]\b",
    "public": r"\bipo\b|initial public offering|\blisted\b|\bnasdaq\b|\bnyse\b|\bnse\b|\bbse\b|went public",
    "acquired": r"acqui",
}


def round_named(value: str, quote: str) -> bool:
    """The quote alone must name the round it is cited for ("logs $30 million" names none)."""
    pat = ROUND_WORDS.get(value)
    return bool(pat and re.search(pat, quote, re.I))


def validate(raw: dict, source: Source) -> tuple[list[dict], list[dict]]:
    """Returns (facts, dropped). Each fact: key, value, quote, source_url, source_type, date, date_basis."""
    errs = schema_errors(raw)
    if errs:
        return [], [{"key": "*", "reason": f"schema: {'; '.join(errs)}", "source_url": source.label}]

    candidates = []                  # (key, value_dict, quote, item, stated_date)
    for k in SIMPLE_KEYS:
        f = raw[k]
        if f["value"] in ("yes", "no"):
            candidates.append((k, {"value": f["value"]}, f["quote"], f["item"], None))
    f = raw["funding_round"]
    if f["value"] != "unknown":
        candidates.append(("funding_round", {"value": f["value"]}, f["quote"], f["item"], f["date"]))
    f = raw["employee_count"]
    lo, hi = _int(f["min"]), _int(f["max"])
    if lo is not None or hi is not None:
        candidates.append(("employee_count", {"value": f["value"], "min": lo if lo is not None else hi,
                                              "max": hi if hi is not None else lo}, f["quote"], f["item"], f["date"]))
    f = raw["soc2_iso_status"]
    if f["value"] in ("attested_type2_or_iso", "type1_only", "in_progress_or_planned"):
        candidates.append(("soc2_iso_status", {"value": f["value"]}, f["quote"], f["item"], None))
    f = raw["security_leader_change"]
    if f["value"] == "yes":
        candidates.append(("security_leader_change", {"value": "yes", "role": f["role"], "person": f["person"]},
                           f["quote"], f["item"], f["date"]))
    for job in raw["compliance_job_titles"]:
        candidates.append(("compliance_job_titles", {"value": job["title"]}, job["quote"], job["item"], None))

    facts, dropped = [], []
    for key, value, quote, item_id, stated in candidates:
        item = source.items.get(item_id) or (source.items.get("none") if len(source.items) == 1 else None)
        if item is None:
            dropped.append({"key": key, "value": value, "quote": quote, "source_url": source.label,
                            "reason": f"item {item_id!r} not in source"})
            continue
        if not quote.strip() or norm(quote) not in norm(item.text):
            dropped.append({"key": key, "value": value, "quote": quote, "source_url": item.url,
                            "reason": "quote not found in source text"})
            continue
        if key == "funding_round" and not round_named(value["value"], quote):
            dropped.append({"key": key, "value": value, "quote": quote, "source_url": item.url,
                            "reason": "quote does not name this round"})
            continue
        stated_d = parse_date(stated)
        facts.append({
            "key": key, "value": value, "quote": quote.strip(), "source_url": item.url,
            "source_type": item.source_type,
            "date": stated_d or item.date,
            "date_basis": "stated" if stated_d else ("published" if item.source_type == "news" else "fetched"),
        })
    return facts, dropped
