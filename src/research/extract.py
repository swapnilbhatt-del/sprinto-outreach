"""LLM extraction into a fixed schema. Every fact carries a verbatim quote; missing facts are "unknown"."""
from __future__ import annotations

from ..llm import CallBudget, JsonLLM

YES_NO = ["yes", "no", "unknown"]


def _fact(value_schema: dict, extra: dict | None = None) -> dict:
    props = {"value": value_schema, "quote": {"type": "string"}, "item": {"type": "string"}}
    props.update(extra or {})
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


SCHEMA = {
    "type": "object",
    "properties": {
        "is_software_product": _fact({"type": "string", "enum": YES_NO}),
        "sells_to_businesses": _fact({"type": "string", "enum": YES_NO}),
        "funding_round": _fact(
            {"type": "string", "enum": ["pre_seed", "seed", "series_a", "series_b", "series_c",
                                        "series_d_plus", "public", "acquired", "unknown"]},
            {"date": {"type": "string"}}),
        "employee_count": _fact({"type": "string"}, {"min": {"type": "string"}, "max": {"type": "string"},
                                                    "date": {"type": "string"}}),
        "soc2_iso_status": _fact({"type": "string", "enum": ["attested_type2_or_iso", "type1_only",
                                                             "in_progress_or_planned", "not_mentioned", "unknown"]}),
        "security_leader_change": _fact({"type": "string", "enum": YES_NO},
                                        {"role": {"type": "string"}, "person": {"type": "string"},
                                         "date": {"type": "string"}}),
        "compliance_job_titles": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"title": {"type": "string"}, "quote": {"type": "string"},
                                     "item": {"type": "string"}},
                      "required": ["title", "quote", "item"], "additionalProperties": False},
        },
        "enterprise_motion": _fact({"type": "string", "enum": YES_NO}),
        "regulated_vertical": _fact({"type": "string", "enum": YES_NO}),
        "eu_presence": _fact({"type": "string", "enum": YES_NO}),
    },
    "additionalProperties": False,
}
SCHEMA["required"] = list(SCHEMA["properties"])

SYSTEM = """You extract facts about ONE company from ONE source document, for B2B sales research.

Rules:
- Only report facts the document states about the given company (name and domain). Ignore other companies with similar names.
- For every fact whose value is not "unknown", `quote` must be copied VERBATIM from the document (a short span, max 200 chars) and must on its own support the value. If you cannot quote it, the value is "unknown" and quote is "".
- Never infer, guess, or use outside knowledge. Absence of a statement is "unknown" (except soc2_iso_status, which is "not_mentioned" when the document is the company's own page and is silent on SOC 2 / ISO 27001).
- `item`: if the document is a numbered news list, the item number the quote comes from (e.g. "3"); otherwise "none".
- Dates: "YYYY-MM" or "YYYY-MM-DD" if stated, else "unknown". employee_count: the company's TOTAL current headcount only (never layoffs, job cuts, hires or open roles); digits in min/max (same number twice for an exact count), "unknown" when not stated; `value` is the raw phrase.
- funding_round: the most recent round named; "acquired" only when this company itself was bought (not when it buys another company), dated by the deal. soc2_iso_status: attested_type2_or_iso = SOC 2 Type II report or ISO 27001 certification already achieved; type1_only = SOC 2 Type I only; in_progress_or_planned = audit underway, working towards, or customers asking for it.
- security_leader_change: "yes" only if a new CISO / head of security / head of compliance / VP security was hired or appointed.
- compliance_job_titles: open roles in compliance, GRC, security, IT audit or privacy. Empty list if none.
- enterprise_motion: enterprise plan, "contact sales" pricing, or named enterprise customers. regulated_vertical: fintech, healthtech, insurance, HR/payroll, or a product whose core is customer PII. eu_presence: EU office, EU customers, or GDPR mentioned."""


def extract_facts(llm: JsonLLM, budget: CallBudget, company: str, source_label: str, text: str) -> dict:
    """`company` is 'Name (domain)'."""
    user = f"Company: {company}\nSource: {source_label}\n\n<document>\n{text}\n</document>"
    return llm.call_json("extract", SYSTEM, user, SCHEMA, budget, max_tokens=4000)
