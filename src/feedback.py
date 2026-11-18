"""L5 feedback loop: make the cold email better every time it's sent.

Signals (all stored in the feedback table):
  - reply outcome, written by the reply agent (interested / confirmed / counter_proposed / not_interested / unclear)
  - the Growth Lead's rating (1-5) of a draft, and the edited version she would actually send
Learning: `propose_lessons` asks the model for at most three short writing rules, each citing the
feedback ids that support it. A lesson is only a candidate until the Growth Lead approves it.
Use: `approved_lessons` are added to the drafting prompt; every draft records which lesson ids it used,
so the next outcome is feedback on those lessons too. The draft checks in draft_email still apply, so
a lesson can change style and focus, never the facts or the rules (one CTA, no links, word limit).
"""
from __future__ import annotations

import difflib

from .llm import CallBudget, JsonLLM, LLMError

MIN_SIGNALS = 2          # don't generalise from a single data point
MAX_LESSON_WORDS = 30
POSITIVE = {"interested", "confirmed", "counter_proposed"}
NEGATIVE = {"not_interested"}

SYSTEM = """You improve a B2B cold-email writer from evidence. You get past drafts with what happened to
each: the prospect's reply outcome, the sales lead's 1-5 rating, and her edited version when she changed it.

Propose at most 3 lessons for writing the NEXT emails better. Rules:
- Each lesson is one imperative instruction about style, structure, tone, length, hook choice or call to
  action, at most 30 words. Example: "Open with the funding round only when it is under 6 months old;
  otherwise open with the compliance hiring signal."
- Each lesson must cite the feedback ids it is based on (at least one), and explain why in one sentence.
- Never put facts about a specific company or person into a lesson, and never contradict: one call to
  action, no links, under 120 words, only verified facts.
- Do not repeat a lesson that is already approved or pending.
- If the evidence doesn't support a clear lesson, return an empty list."""

SCHEMA = {
    "type": "object",
    "properties": {"lessons": {"type": "array", "items": {
        "type": "object",
        "properties": {"text": {"type": "string"}, "rationale": {"type": "string"},
                       "evidence": {"type": "array", "items": {"type": "integer"}}},
        "required": ["text", "rationale", "evidence"], "additionalProperties": False}}},
    "required": ["lessons"], "additionalProperties": False,
}


def edit_summary(original: str, edited: str, limit: int = 1200) -> str:
    diff = difflib.unified_diff(original.splitlines(), edited.splitlines(), "draft", "edited", lineterm="", n=0)
    return "\n".join(list(diff)[2:])[:limit]


def evidence_items(data) -> list[dict]:
    """Feedback rows joined with the draft they are about."""
    trails = {t["domain"]: t for t in data.list_trails()}
    items = []
    for f in data.feedback_rows():
        t = trails.get(f["domain"]) or {}
        d = (t.get("email") or {}).get("draft") or {}
        if not d:
            continue
        has_signal = f.get("rating") or f.get("edited_body") or f.get("reply_status") in POSITIVE | NEGATIVE
        if not has_signal:
            continue
        items.append({"id": f["id"], "domain": f["domain"], "rating": f.get("rating"),
                      "reply_status": f.get("reply_status"), "notes": f.get("notes"),
                      "lessons_used": f.get("lessons_used") or [], "subject": d.get("subject"), "body": d.get("body"),
                      "edit": edit_summary(d.get("body", ""), f["edited_body"]) if f.get("edited_body") else None})
    return items


def propose_lessons(data, llm: JsonLLM) -> dict:
    items = evidence_items(data)
    if len(items) < MIN_SIGNALS:
        return {"ok": False, "error": f"Need at least {MIN_SIGNALS} rated, edited or replied drafts to learn from "
                                      f"(have {len(items)})."}
    known = data.lessons()
    known_txt = "\n".join(f"- [{l['status']}] {l['text']}" for l in known if l["status"] != "rejected") or "(none)"
    blocks = []
    for it in items[:30]:
        blocks.append(f"### feedback id {it['id']}\noutcome: {it['reply_status'] or 'none yet'} | rating: "
                      f"{it['rating'] or 'none'} | notes: {it['notes'] or ''} | lessons used: {it['lessons_used']}\n"
                      f"subject: {it['subject']}\ndraft:\n{it['body']}\n"
                      + (f"her edit (diff):\n{it['edit']}\n" if it["edit"] else ""))
    user = f"Existing lessons:\n{known_txt}\n\nEvidence:\n\n" + "\n".join(blocks)
    try:
        raw = llm.call_json("lessons", SYSTEM, user, SCHEMA, CallBudget(2), max_tokens=1500)
    except LLMError as e:
        return {"ok": False, "error": f"model call failed: {e}"}
    ids = {it["id"] for it in items}
    seen = {l["text"].strip().lower() for l in known}
    added, dropped = [], []
    for l in raw.get("lessons", [])[:3]:
        text = " ".join((l.get("text") or "").split())
        ev = [i for i in l.get("evidence", []) if i in ids]
        why = None
        if not text:
            why = "empty"
        elif len(text.split()) > MAX_LESSON_WORDS:
            why = f"longer than {MAX_LESSON_WORDS} words"
        elif not ev:
            why = "cites no real feedback"
        elif text.lower() in seen:
            why = "duplicate"
        if why:
            dropped.append({"text": text, "reason": why})
            continue
        seen.add(text.lower())
        added.append(data.add_lesson(text, l.get("rationale", ""), ev))
    return {"ok": True, "added": added, "dropped": dropped, "evidence_count": len(items)}


def approved_lessons(data) -> list[dict]:
    return [{"id": l["id"], "text": l["text"]} for l in data.lessons("approved")]
