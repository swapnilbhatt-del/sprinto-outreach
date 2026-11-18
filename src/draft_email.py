"""Step 7: draft a personalised email from validated facts only, then check it mechanically."""
from __future__ import annotations

import re

from .llm import BudgetExceeded, CallBudget, JsonLLM, LLMError

SYSTEM = """You write a short, specific cold email from a Sprinto account executive to one named person.

Hard rules:
- Use ONLY the facts provided (each has an id, a quote and a source URL). Do not add any other claim about the company or person: no numbers, customers, tools, events or dates that are not in a provided fact.
- Open with a hook built on ONE provided fact (give its id as hook_fact_id). Make it concrete and checkable.
- Address the person by first name in the greeting.
- Pick the two pain points most likely for this person's role, each tied to a provided fact id.
- Body under {max_words} words, plain text, no links, no bullet lists, no exclamation marks, no flattery.
- Exactly one call to action, phrased as a low-friction question (e.g. a 15-minute call). Put the same sentence in `cta`.
- Sign off as "— Sprinto team".
- If none of the facts makes a specific hook, set hook_fact_id to -1 and leave subject/body empty."""

SCHEMA = {
    "type": "object",
    "properties": {
        "hook_fact_id": {"type": "integer"},
        "pain_points": {"type": "array", "items": {
            "type": "object",
            "properties": {"pain": {"type": "string"}, "fact_id": {"type": "integer"}, "why_for_role": {"type": "string"}},
            "required": ["pain", "fact_id", "why_for_role"], "additionalProperties": False}},
        "subject": {"type": "string"},
        "body": {"type": "string"},
        "cta": {"type": "string"},
        "reasoning": {"type": "string"},
    },
    "required": ["hook_fact_id", "pain_points", "subject", "body", "cta", "reasoning"],
    "additionalProperties": False,
}


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'’-]+\b", text))


def hook_candidates(facts: list[dict], keys: list[str]) -> list[dict]:
    out = []
    for f in facts:
        if f["key"] not in keys:
            continue
        if f["key"] == "soc2_iso_status" and f["value"]["value"] != "in_progress_or_planned":
            continue
        out.append(f)
    return out


def check_draft(d: dict, contact: dict, facts_by_id: dict, hooks_by_id: dict, max_words: int) -> list[str]:
    problems = []
    body = d.get("body", "")
    if d.get("hook_fact_id") not in hooks_by_id:
        problems.append(f"hook_fact_id {d.get('hook_fact_id')} is not a hook-eligible fact")
    if not re.search(rf"\b{re.escape(contact['first_name'])}\b", body.split("\n", 1)[0]):
        problems.append("greeting does not use the contact's first name")
    if word_count(body) >= max_words:
        problems.append(f"body is {word_count(body)} words (limit < {max_words})")
    if not d.get("cta") or d["cta"].strip() not in body:
        problems.append("cta missing or not present verbatim in body")
    if body.count("?") != 1:
        problems.append(f"expected exactly one question (the CTA), found {body.count('?')}")
    if re.search(r"https?://", body):
        problems.append("body contains a link")
    pps = d.get("pain_points", [])
    if len(pps) != 2 or any(p.get("fact_id") not in facts_by_id for p in pps):
        problems.append("need exactly two pain points, each tied to a provided fact id")
    return problems


def draft(llm: JsonLLM, budget: CallBudget, icp: dict, domain: str, contact: dict, facts: list[dict],
          contact_reason: str, lessons: list[dict] | None = None) -> tuple[str, dict | None, str]:
    """Returns (status, draft, reason). status: drafted | needs_review."""
    ecfg = icp["email"]
    hooks = hook_candidates(facts, ecfg["hook_fact_keys"])
    if not hooks:
        return "needs_review", None, "no specific, checkable fact to hook on"
    facts_by_id = {f["id"]: f for f in facts}
    hooks_by_id = {f["id"]: f for f in hooks}
    fact_lines = "\n".join(
        f"- id {f['id']} [{f['key']}{' — HOOK-ELIGIBLE' if f['id'] in hooks_by_id else ''}] "
        f"value={f['value']} date={f.get('date')} quote=\"{f['quote']}\" source={f['source_url']}"
        for f in facts)
    user = (f"Company: {domain}\n"
            f"Recipient: {contact['first_name']} {contact.get('last_name') or ''}, {contact['position']}\n"
            f"Why this person: {contact_reason}\n\n"
            f"What Sprinto does:\n" + "\n".join(f"- {v}" for v in ecfg["sprinto_value_props"]) +
            f"\n\nVerified facts (the only facts you may use):\n{fact_lines}")
    system = SYSTEM.format(max_words=ecfg["max_words"])
    if lessons:
        # L5: approved lessons from past outcomes. They shape style and focus; the hard rules above still win.
        system += ("\n\nLessons from past outcomes, approved by the Growth Lead (follow them unless they "
                   "conflict with a hard rule above):\n" + "\n".join(f"- {l['text']}" for l in lessons))
    last_problems = []
    for attempt in range(2):
        if attempt:
            user += "\n\nYour previous draft failed these checks; fix them:\n- " + "\n- ".join(last_problems)
        try:
            d = llm.call_json("draft", system, user, SCHEMA, budget, max_tokens=3000)
        except (LLMError, BudgetExceeded) as e:
            return "needs_review", None, f"draft call failed: {e}"
        if d.get("hook_fact_id") == -1:
            return "needs_review", None, f"model found no specific hook: {d.get('reasoning', '')}"
        last_problems = check_draft(d, contact, facts_by_id, hooks_by_id, ecfg["max_words"])
        if not last_problems:
            hook = hooks_by_id[d["hook_fact_id"]]
            d["hook_url"] = hook["source_url"]
            d["lessons_used"] = [l["id"] for l in lessons or []]
            d["hook_quote"] = hook["quote"]
            return "drafted", d, d.get("reasoning", "")
    return "needs_review", None, "draft failed checks twice: " + "; ".join(last_problems)


def render_for_review(domain: str, contact: dict, d: dict, contact_reason: str, gate_summary: str) -> str:
    """The message that actually goes to OWN_EMAIL: the draft, then the evidence behind it."""
    pains = "\n".join(f"  {i + 1}. {p['pain']} (fact {p['fact_id']}) — {p['why_for_role']}"
                      for i, p in enumerate(d["pain_points"]))
    return (f"{d['body']}\n\n"
            f"──────── internal notes (not part of the prospect email) ────────\n"
            f"Prospect: {contact['first_name']} {contact.get('last_name') or ''} <{contact['email']}>, "
            f"{contact['position']} at {domain}\n"
            f"Why this person: {contact_reason}\n"
            + (f"⚠ NAME CHECK: {contact['name_warning']}\n" if contact.get("name_warning") else "")
            + f"Hook (fact {d['hook_fact_id']}): \"{d['hook_quote']}\"\n  Source: {d['hook_url']}\n"
            f"Pain points:\n{pains}\n"
            f"Why it was written this way: {d.get('reasoning', '')}\n"
            f"Qualification: {gate_summary}\n")
