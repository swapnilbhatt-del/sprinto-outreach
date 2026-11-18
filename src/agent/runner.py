"""Wires the reply agent to real services and runs it.

    start_thread(data, trail)   send the approved draft (to OWN_EMAIL) and open a thread in agent_threads
    tick(data)                  run the graph once for every active thread (called by the scheduler)

Errors are mapped to the retry table's two kinds: TransientToolError (timeouts, 429, 5xx) and
PermanentToolError (not configured, refused, 4xx).
"""
from __future__ import annotations

import os
import socket
from datetime import datetime

import httpx

from ..llm import CallBudget, LLMError, make_llm
from ..send_gmail import RecipientNotAllowed
from .graph import build_graph, new_state
from .tools import TZ, PermanentToolError, Toolbox, TransientToolError, run_tool

HUBSPOT = "https://api.hubapi.com"


def _wrap(fn):
    """Call a Google/HubSpot function and translate its failure into transient or permanent."""
    def inner(*a, **k):
        from .. import google_client
        try:
            return fn(*a, **k)
        except (RecipientNotAllowed, google_client.GoogleNotConfigured) as e:
            raise PermanentToolError(str(e)) from e
        except (socket.timeout, TimeoutError, httpx.TimeoutException, httpx.TransportError) as e:
            raise TransientToolError(f"{type(e).__name__}: {e}") from e
        except Exception as e:                                 # googleapiclient.errors.HttpError and others
            status = getattr(getattr(e, "resp", None), "status", None) or getattr(getattr(e, "response", None), "status_code", None)
            if status and (int(status) == 429 or int(status) >= 500):
                raise TransientToolError(f"HTTP {status}") from e
            if isinstance(e, (TransientToolError, PermanentToolError)):
                raise
            raise PermanentToolError(f"{type(e).__name__}: {e}") from e
    return inner


# ------------------------------------------------------------------ HubSpot (update_crm)
def hubspot_update(domain: str, line: str, lifecycle: str | None, token: str | None = None,
                   http: httpx.Client | None = None) -> dict:
    token = token or os.environ.get("HUBSPOT_TOKEN")
    if not token:
        raise PermanentToolError("HUBSPOT_TOKEN is not set")
    c = http or httpx.Client(timeout=20, headers={"Authorization": f"Bearer {token}"})

    def check(r):
        if r.status_code == 429 or r.status_code >= 500:
            raise TransientToolError(f"HubSpot HTTP {r.status_code}")
        if r.status_code >= 400:
            raise PermanentToolError(f"HubSpot HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    found = check(c.post(f"{HUBSPOT}/crm/v3/objects/companies/search", json={
        "filterGroups": [{"filters": [{"propertyName": "domain", "operator": "EQ", "value": domain}]}],
        "properties": ["description", "lifecyclestage", "name"], "limit": 1}))
    stamp = datetime.now(TZ).strftime("%Y-%m-%d")
    entry = f"[{stamp} reply agent] {line}"
    if found.get("results"):
        comp = found["results"][0]
        desc = (comp["properties"].get("description") or "").strip()
        props = {"description": (desc + "\n" + entry).strip()[-65000:]}
        if lifecycle:
            props["lifecyclestage"] = lifecycle
        check(c.patch(f"{HUBSPOT}/crm/v3/objects/companies/{comp['id']}", json={"properties": props}))
        return {"company_id": comp["id"], "updated": True}
    props = {"domain": domain, "name": domain.split(".")[0].title(), "description": entry}
    if lifecycle:
        props["lifecyclestage"] = lifecycle
    made = check(c.post(f"{HUBSPOT}/crm/v3/objects/companies", json={"properties": props}))
    return {"company_id": made["id"], "updated": True}


# ------------------------------------------------------------------ reply classification
CLASSIFY_SYSTEM = """You read one email reply from a sales prospect and classify it. Today is {today} ({tz}).

Intents:
- interested: wants to talk or learn more, no specific time chosen
- confirm: accepts one of the offered slots (give its 0-based index)
- counter_propose: suggests a different time (give it as ISO 8601 with offset, e.g. 2026-11-24T15:00:00+05:30)
- not_interested: declines, asks to stop, or says no need
- unclear: anything else, or you are not sure

Never guess a slot index or a time that the reply does not state. If unsure, answer unclear."""

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {"intent": {"type": "string", "enum": ["interested", "confirm", "counter_propose",
                                                         "not_interested", "unclear"]},
                   "slot_index": {"type": "integer"}, "proposed_time": {"type": "string"},
                   "reason": {"type": "string"}},
    "required": ["intent", "slot_index", "proposed_time", "reason"], "additionalProperties": False,
}


def classify_reply(llm, text: str, offered: list[str]) -> dict:
    from .tools import fmt_slot
    now = datetime.now(TZ)
    slots = "\n".join(f"{i}: {s} ({fmt_slot(s)})" for i, s in enumerate(offered)) or "(none offered yet)"
    user = f"Slots we offered:\n{slots}\n\nThe reply:\n<reply>\n{text[:3000]}\n</reply>"
    try:
        out = llm.call_json("classify", CLASSIFY_SYSTEM.format(today=now.strftime("%A %d %B %Y"), tz="IST"),
                            user, CLASSIFY_SCHEMA, CallBudget(2), max_tokens=600)
    except LLMError as e:
        return {"intent": "unclear", "slot_index": -1, "proposed_time": "", "reason": f"classifier failed: {e}"}
    if out.get("intent") == "counter_propose":
        try:
            t = datetime.fromisoformat(out.get("proposed_time", ""))
            out["proposed_time"] = (t if t.tzinfo else t.replace(tzinfo=TZ)).isoformat()
        except ValueError:
            out["proposed_time"] = ""
    return out


# ------------------------------------------------------------------ real toolbox
def real_toolbox(llm=None) -> Toolbox:
    from .. import google_client as g
    from ..config import load_icp
    llm = llm or make_llm(load_icp()["llm"])
    own = os.environ.get("OWN_EMAIL", "")
    return Toolbox(
        send=_wrap(lambda subject, body, thread_id, reply_to: g.send_email(
            own, subject, body, thread_id=thread_id, reply_to_message_id=reply_to, kind="agent")),
        replies=_wrap(g.thread_replies),
        busy=_wrap(g.calendar_busy),
        book=_wrap(lambda start, end, summary, description: g.calendar_book(start, end, summary, description)),
        crm=hubspot_update,
        classify=lambda text, offered: classify_reply(llm, text, offered),
    )


def start_thread(data, trail: dict, tb: Toolbox | None = None, lessons_used: list | None = None) -> dict:
    """Send the drafted outreach (to OWN_EMAIL) and open the agent thread for this company."""
    from ..draft_email import render_for_review
    d, c = (trail.get("email") or {}).get("draft"), (trail.get("contact") or {}).get("chosen")
    if not d or not c:
        return {"ok": False, "error": "This account has no drafted email and contact."}
    existing = data.get_thread(trail["domain"])
    if existing and existing["status"] == "active":
        return {"ok": False, "error": "The agent is already running a thread for this account."}
    tb = tb or real_toolbox()
    gates = "; ".join(f"{g} {v['status']}" for g, v in (trail.get("gates") or {}).items())
    body = render_for_review(trail["domain"], c, d, (trail.get("contact") or {}).get("why", ""), gates)
    body += ("\n\nReply to this email as the prospect would (e.g. \"interested, what times work?\", "
             "\"Tuesday 3pm instead?\", \"not now\"). The reply agent reads your reply on its next check.")
    events = []
    res = run_tool("send_email", lambda: tb.send(f"[Sprinto draft → {c['first_name']} @ {trail['domain']}] {d['subject']}",
                                                 body, None, None),
                   lambda tool, outcome, detail: events.append((tool, outcome, detail)))
    for tool, outcome, detail in events:
        data.event(trail["domain"], "start", outcome, tool, detail)
    if not res.ok:
        return {"ok": False, "error": f"send failed ({res.action}): {res.error}"}
    state = new_state(trail["domain"], (trail.get("company") or {}).get("name") or trail["domain"],
                      {k: c.get(k) for k in ("first_name", "last_name", "position", "email")}, res.value, tb.now())
    state["lessons_used"] = lessons_used or []
    data.put_thread(trail["domain"], dict(state), "active")
    return {"ok": True, "thread_id": state["thread_id"]}


def tick(data, tb: Toolbox | None = None, domain: str | None = None, no_reply_hours: float | None = None) -> list[dict]:
    """Run each active thread's graph once; persist state and events. Returns a summary per thread."""
    threads = [t for t in data.list_threads("active") if not domain or t["domain"] == domain]
    if not threads:
        return []
    tb = tb or real_toolbox()
    hours = no_reply_hours if no_reply_hours is not None else float(os.environ.get("AGENT_NO_REPLY_HOURS", 48))
    graph = build_graph(tb, no_reply_hours=hours)
    out = []
    for t in threads:
        before = t["state"]
        state = {**before, "log": [], "route": ""}
        after = graph.invoke(state)
        for e in after.get("log") or []:
            data.event(t["domain"], e["node"], e["outcome"], e.get("tool"), e.get("detail"))
        if after.get("reply_status") != before.get("reply_status") and after.get("reply_status") not in ("no_reply", None):
            # L5: every outcome is feedback on the email that produced it
            data.add_feedback(t["domain"], None, None, None, after["reply_status"], before.get("lessons_used") or [])
        status = after.get("thread_status", "active")
        clean = {k: v for k, v in after.items() if k not in ("log", "route")}
        data.put_thread(t["domain"], clean, "active" if status == "active" else status)
        out.append({"domain": t["domain"], "status": status, "reply_status": after.get("reply_status"),
                    "meeting_status": after.get("meeting_status"), "events": len(after.get("log") or [])})
    return out
