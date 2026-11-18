"""L6 reply agent as a LangGraph state machine.

State object: the six fields from L6 (reply_status, calendar_checked, times_proposed, reply_round,
meeting_status, crm_logged) plus the thread's context. Every node reads the state and returns the
fields it changed; LangGraph merges them, so no value is threaded through by hand.

One `tick` runs the graph once for a thread, from read_inbox to a stop:
  read_inbox --no reply--> (wait)                 --48h silence--> escalate
             --not interested--> update_crm
             --interested--> check_calendar -> propose_time -> (wait)
             --counter-proposal--> counter (reply_round += 1; at 2 -> escalate) -> check_calendar -> propose_time
             --confirms a slot--> book_meeting -> update_crm
             --unclear--> escalate -> update_crm
Between ticks the state is saved in the agent_threads table (src/agent/runner.py), so the agent can
stop at any wait and resume on the next tick: that table is its checkpoint.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .tools import TOOL_DESCRIPTIONS, Toolbox, candidate_slots, fmt_slot, free_slots, run_tool

MAX_ROUNDS = 2              # L6: reply_round reaching 2 without confirmation -> escalate
NO_REPLY_HOURS = 48         # L6: no reply within 48 hours -> escalate


class AgentState(TypedDict, total=False):
    # the six L6 fields
    reply_status: str          # no_reply | interested | not_interested | counter_proposed | confirmed | unclear
    calendar_checked: bool
    times_proposed: list[str]
    reply_round: int
    meeting_status: str        # unscheduled | pending_confirmation | confirmed
    crm_logged: bool
    # context the tools need
    domain: str
    company: str
    contact: dict
    thread_id: str
    last_rfc_id: str
    last_sent_at: str
    seen_reply_ids: list[str]
    offered_history: list[str]
    requested_time: str
    confirmed_time: str
    last_reply_text: str
    escalation_reason: str
    thread_status: str         # active | done | escalated | aborted
    lessons_used: list[int]    # L5: approved lessons the outreach draft was written with
    escalated_notified: bool   # the escalation email has gone out
    crm_attempted: bool        # update_crm already ran (so a CRM failure escalates once, without looping)
    route: str                 # where the router sends the graph next (internal)
    log: list[dict]            # events written this tick (the runner persists them)


def new_state(domain: str, company: str, contact: dict, sent: dict, now: datetime) -> AgentState:
    return AgentState(reply_status="no_reply", calendar_checked=False, times_proposed=[], reply_round=0,
                      meeting_status="unscheduled", crm_logged=False, domain=domain, company=company,
                      contact=contact, thread_id=sent["threadId"], last_rfc_id=sent.get("rfc_id") or "",
                      last_sent_at=now.isoformat(), seen_reply_ids=[], offered_history=[], requested_time="",
                      confirmed_time="", last_reply_text="", escalation_reason="", thread_status="active",
                      route="", log=[])


def build_graph(tb: Toolbox, no_reply_hours: float = NO_REPLY_HOURS):
    def ev(s, node, outcome, tool=None, detail=None):
        return [*(s.get("log") or []), {"node": node, "tool": tool, "outcome": outcome, "detail": detail}]

    def tool_logger(s, node, sink):
        return lambda tool, outcome, detail: sink.append({"node": node, "tool": tool, "outcome": outcome,
                                                           "detail": detail})

    def failed(s, node, res, sink):
        """Turn a ToolResult failure into the route the retry table chose."""
        log = [*(s.get("log") or []), *sink]
        if res.action == "escalate":
            return {"route": "escalate", "escalation_reason": f"{node}: {res.error}", "log": log}
        if res.action == "abort":
            return {"route": "end", "thread_status": "aborted", "escalation_reason": f"{node}: {res.error}", "log": log}
        return {"route": "skip", "log": log}

    # ------------------------------------------------------------------ nodes
    def read_inbox(s: AgentState):
        try:
            replies = tb.replies(s["thread_id"])
        except Exception as e:                       # can't read the inbox this tick: try again next tick
            return {"route": "wait", "log": ev(s, "read_inbox", "inbox_error", detail=str(e))}
        new = [r for r in replies if r["id"] not in (s.get("seen_reply_ids") or [])]
        if not new:
            sent = datetime.fromisoformat(s["last_sent_at"])
            if tb.now() - sent >= timedelta(hours=no_reply_hours):
                return {"route": "escalate", "escalation_reason": f"no reply within {no_reply_hours:g} hours",
                        "log": ev(s, "read_inbox", "silence_timeout")}
            return {"route": "wait", "log": ev(s, "read_inbox", "no_new_reply")}
        r = new[-1]
        c = tb.classify(r["text"], s.get("times_proposed") or [])
        intent = c.get("intent", "unclear")
        upd = {"seen_reply_ids": [*(s.get("seen_reply_ids") or []), *(x["id"] for x in new)],
               "last_rfc_id": r.get("rfc_id") or s.get("last_rfc_id"), "last_reply_text": r["text"][:1000]}
        detail = f"{intent}: {c.get('reason', '')}"
        if intent == "not_interested":
            return {**upd, "reply_status": "not_interested", "route": "update_crm",
                    "log": ev(s, "read_inbox", "classified", detail=detail)}
        if intent == "confirm":
            idx, offered = c.get("slot_index", -1), s.get("times_proposed") or []
            if isinstance(idx, int) and 0 <= idx < len(offered):
                return {**upd, "reply_status": "confirmed", "confirmed_time": offered[idx], "route": "book_meeting",
                        "log": ev(s, "read_inbox", "classified", detail=detail)}
            intent = "unclear"                       # "yes" without a slot we offered: a human should look
        if intent == "counter_propose":
            return {**upd, "reply_status": "counter_proposed", "requested_time": c.get("proposed_time") or "",
                    "route": "counter", "log": ev(s, "read_inbox", "classified", detail=detail)}
        if intent == "interested":
            return {**upd, "reply_status": "interested", "route": "check_calendar",
                    "log": ev(s, "read_inbox", "classified", detail=detail)}
        return {**upd, "reply_status": "unclear", "route": "escalate",
                "escalation_reason": "reply could not be classified with confidence",
                "log": ev(s, "read_inbox", "classified", detail=detail)}

    def counter(s: AgentState):
        rnd = (s.get("reply_round") or 0) + 1
        if rnd >= MAX_ROUNDS:
            return {"reply_round": rnd, "route": "escalate",
                    "escalation_reason": f"counter-proposal round {rnd} without a confirmed time",
                    "log": ev(s, "counter", "round_cap")}
        return {"reply_round": rnd, "calendar_checked": False, "route": "check_calendar",
                "log": ev(s, "counter", f"round {rnd}")}

    def check_calendar(s: AgentState):
        sink: list = []
        now = tb.now()
        req = s.get("requested_time") or ""

        def call():
            cands = candidate_slots(now)
            if req:
                try:
                    t = datetime.fromisoformat(req)
                    if now < t <= now + timedelta(days=14):
                        cands = [t]
                except ValueError:
                    pass
            busy = tb.busy(min(cands), max(cands) + timedelta(minutes=30))
            return free_slots(cands, busy, exclude=s.get("offered_history") or [])

        res = run_tool("check_calendar", call, tool_logger(s, "check_calendar", sink))
        if not res.ok:
            return failed(s, "check_calendar", res, sink)
        slots = res.value
        if not slots and req:                       # the asked-for time is taken: offer the normal set
            res = run_tool("check_calendar", lambda: free_slots(candidate_slots(now), tb.busy(
                now, now + timedelta(days=8)), exclude=s.get("offered_history") or []),
                tool_logger(s, "check_calendar", sink))
            if not res.ok:
                return failed(s, "check_calendar", res, sink)
            slots = res.value
        log = [*(s.get("log") or []), *sink]
        if not slots:
            return {"calendar_checked": True, "route": "escalate", "escalation_reason": "no free slot to offer",
                    "log": log}
        return {"calendar_checked": True, "times_proposed": slots, "route": "propose_time", "log": log}

    def propose_time(s: AgentState):
        if not s.get("calendar_checked"):
            return {"route": "check_calendar", "log": ev(s, "propose_time", "needs_fresh_calendar_check")}
        slots = s["times_proposed"]
        first = (s.get("contact") or {}).get("first_name") or "there"
        lines = "\n".join(f"  • {fmt_slot(x)}" for x in slots)
        lead = ("Thanks for suggesting a time. That one works:" if len(slots) == 1 and s.get("requested_time")
                else "Thanks for getting back to me. Here are a few times that work on my side:")
        body = (f"Hi {first},\n\n{lead}\n{lines}\n\nReply with the one that suits you, or suggest another time."
                f"\n\n— Sprinto team")
        sink: list = []
        res = run_tool("propose_time", lambda: tb.send(f"Re: times for a quick call ({s.get('company')})", body,
                                                       s["thread_id"], s.get("last_rfc_id")),
                       tool_logger(s, "propose_time", sink))
        if not res.ok:
            return failed(s, "propose_time", res, sink)
        return {"meeting_status": "pending_confirmation", "offered_history": [*(s.get("offered_history") or []), *slots],
                "last_sent_at": tb.now().isoformat(), "requested_time": "", "route": "wait",
                "log": [*(s.get("log") or []), *sink]}

    def book_meeting(s: AgentState):
        start = datetime.fromisoformat(s["confirmed_time"])
        c = s.get("contact") or {}
        sink: list = []
        res = run_tool("book_meeting", lambda: tb.book(
            start, start + timedelta(minutes=30), f"Sprinto intro call: {c.get('first_name', '')} ({s.get('company')})",
            f"Booked by the reply agent from thread {s['thread_id']}. Prospect: {c.get('position', '')} at {s['domain']}."),
            tool_logger(s, "book_meeting", sink))
        if not res.ok:
            return failed(s, "book_meeting", res, sink)
        body = (f"Hi {c.get('first_name') or 'there'},\n\nConfirmed for {fmt_slot(s['confirmed_time'])}. "
                f"Looking forward to it.\n\n— Sprinto team")
        res2 = run_tool("send_email", lambda: tb.send(f"Confirmed: {fmt_slot(s['confirmed_time'])}", body,
                                                      s["thread_id"], s.get("last_rfc_id")),
                        tool_logger(s, "book_meeting", sink))
        log = [*(s.get("log") or []), *sink]
        if not res2.ok and res2.action == "abort":
            return {"meeting_status": "confirmed", "route": "end", "thread_status": "aborted", "log": log}
        return {"meeting_status": "confirmed", "route": "update_crm", "log": log}

    def escalate(s: AgentState):
        c = s.get("contact") or {}
        body = (f"The reply agent stopped on {s.get('company')} ({s['domain']}) and needs you.\n\n"
                f"Reason: {s.get('escalation_reason')}\n"
                f"Contact: {c.get('first_name', '')} {c.get('last_name') or ''}, {c.get('position', '')}\n"
                f"State: reply_status={s.get('reply_status')}, reply_round={s.get('reply_round')}, "
                f"meeting_status={s.get('meeting_status')}, times_proposed={s.get('times_proposed')}\n\n"
                f"Last reply:\n{s.get('last_reply_text') or '(none)'}")
        sink: list = []
        run_tool("send_email", lambda: tb.send(f"[Escalation] {s.get('company')}: {s.get('escalation_reason')}",
                                               body, None, None), tool_logger(s, "escalate", sink))
        nxt = "end" if s.get("crm_attempted") else "update_crm"     # never loop back into a failed CRM call
        return {"thread_status": "escalated", "escalated_notified": True, "route": nxt,
                "log": [*(s.get("log") or []), *sink]}

    def update_crm(s: AgentState):
        if s.get("crm_logged"):
            return {"route": "end", "log": ev(s, "update_crm", "already_logged")}
        if s.get("meeting_status") == "confirmed":
            line, stage, status = f"Meeting booked for {fmt_slot(s['confirmed_time'])}.", "opportunity", "done"
        elif s.get("thread_status") == "escalated":
            line, stage, status = f"Handed to Growth Lead: {s.get('escalation_reason')}.", None, "escalated"
        else:
            line, stage, status = "Prospect replied: not interested.", None, "done"
        sink: list = []
        res = run_tool("update_crm", lambda: tb.crm(s["domain"], line, stage), tool_logger(s, "update_crm", sink))
        log = [*(s.get("log") or []), *sink]
        if not res.ok:
            # The retry table says a human logs it: email the Growth Lead (escalate), then stop.
            base = s.get("escalation_reason") or ""
            return {"crm_logged": False, "crm_attempted": True, "thread_status": "escalated",
                    "escalation_reason": (base + " | " if base else "") + f"CRM not updated ({res.error}); log it by hand: {line}",
                    "route": "end" if s.get("thread_status") == "escalated" and s.get("escalated_notified") else "escalate",
                    "log": log}
        return {"crm_logged": True, "thread_status": status, "route": "end", "log": log}

    # ------------------------------------------------------------------ wiring
    g = StateGraph(AgentState)
    for name, fn in [("read_inbox", read_inbox), ("counter", counter), ("check_calendar", check_calendar),
                     ("propose_time", propose_time), ("book_meeting", book_meeting), ("escalate", escalate),
                     ("update_crm", update_crm)]:
        g.add_node(name, fn)
    g.add_edge(START, "read_inbox")
    targets = {"wait": END, "end": END, "skip": END, "escalate": "escalate", "update_crm": "update_crm",
               "check_calendar": "check_calendar", "counter": "counter", "book_meeting": "book_meeting",
               "propose_time": "propose_time"}
    for node in ["read_inbox", "counter", "check_calendar", "propose_time", "book_meeting", "escalate", "update_crm"]:
        g.add_conditional_edges(node, lambda s: s.get("route", "end"), targets)
    return g.compile()


__all__ = ["AgentState", "build_graph", "new_state", "TOOL_DESCRIPTIONS", "MAX_ROUNDS", "NO_REPLY_HOURS"]
