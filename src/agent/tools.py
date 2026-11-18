"""The reply agent's five tools (L6), their descriptions, and the retry/abort table.

Each description answers the four questions from L6: what it does, when to use it, when not to use
it, and what it returns. The graph (src/agent/graph.py) decides which tool runs; `run_tool` applies
the retry table so a failure is handled the way it was decided before the build, not improvised.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("AGENT_TIMEZONE", "Asia/Kolkata"))


class TransientToolError(RuntimeError):
    """Trying again could plausibly help: timeouts, rate limits, 5xx."""


class PermanentToolError(RuntimeError):
    """Trying again changes nothing: not configured, refused recipient, 4xx, bad input."""


TOOL_DESCRIPTIONS = {
    "send_email": (
        "Sends one plain-text email in the prospect's thread. Use to send the first outreach draft and "
        "to answer a reply that needs no calendar step. Do not use to propose meeting times (use "
        "propose_time) and never for any address other than OWN_EMAIL; the code refuses others. "
        "Returns: {sent: true, message_id, thread_id} or raises."),
    "check_calendar": (
        "Checks your Google Calendar for free 30-minute slots in working hours (10:00-17:00, Mon-Fri) over "
        "the next 5 working days, or checks one specific time the prospect asked for. Use when the prospect "
        "is interested or counter-proposes a time. Do not use to list meetings, and not for dates more "
        "than 14 days out; those aren't bookable yet. Returns: free slots (ISO times), or [] when none fit."),
    "propose_time": (
        "Emails the prospect up to three free slots in the same thread and records them in times_proposed. "
        "Use right after check_calendar returned free slots. Do not use without a fresh calendar check "
        "(calendar_checked must be true), and never re-offer a slot already rejected. "
        "Returns: {sent: true, times_proposed: [...]}."),
    "book_meeting": (
        "Creates the meeting on your own calendar for the time the prospect confirmed, then sends a "
        "confirmation in the thread. Use only when the reply confirms one of times_proposed. Do not use on "
        "an unconfirmed or counter-proposed time. Adds no attendees: the prospect is simulated in this "
        "build. Returns: {event_id, start}."),
    "update_crm": (
        "Writes the outcome to the company's HubSpot record (lifecycle stage and a dated line in its "
        "description). Use once per thread when it ends: meeting booked, not interested, or escalated. "
        "Do not use for intermediate steps. Returns: {company_id, updated: true}."),
}

# The L6 grid, filled in per tool before the build:
#   transient -> (attempts, then)   permanent -> action
#   skip = carry on without it; escalate = hand the thread to the Growth Lead; abort = stop the thread.
RETRY_TABLE = {
    "send_email":     {"transient": (3, "skip"),     "permanent": "abort"},     # nothing downstream needs it
    "check_calendar": {"transient": (3, "escalate"), "permanent": "escalate"},  # propose_time depends on it
    "propose_time":   {"transient": (3, "escalate"), "permanent": "escalate"},  # human can propose by hand
    "book_meeting":   {"transient": (3, "abort"),    "permanent": "abort"},     # no substitute mid-run
    "update_crm":     {"transient": (3, "escalate"), "permanent": "escalate"},  # human can log it manually
}


@dataclass
class ToolResult:
    ok: bool
    value: object = None
    action: str | None = None          # None on success, else skip | escalate | abort
    error: str | None = None
    attempts: int = 0


def run_tool(name: str, fn: Callable[[], object], log: Callable[[str, str, str], None] | None = None,
             sleep: Callable[[float], None] | None = None) -> ToolResult:
    """Run one tool call under its retry-table row."""
    import time
    sleep = sleep or time.sleep
    attempts, then = RETRY_TABLE[name]["transient"]
    for i in range(1, attempts + 1):
        try:
            v = fn()
            if log:
                log(name, "ok", f"attempt {i}")
            return ToolResult(True, v, attempts=i)
        except PermanentToolError as e:
            action = RETRY_TABLE[name]["permanent"]
            if log:
                log(name, f"permanent_error -> {action}", str(e))
            return ToolResult(False, action=action, error=str(e), attempts=i)
        except TransientToolError as e:
            if log:
                log(name, "transient_error", f"attempt {i}: {e}")
            if i < attempts:
                sleep(min(2 ** i, 8))
    return ToolResult(False, action=then, error=f"transient failure after {attempts} attempts", attempts=attempts)


# ------------------------------------------------------------------ slot logic (pure, testable)
def candidate_slots(now: datetime, days: int = 5, start_h: int = 10, end_h: int = 17) -> list[datetime]:
    """30-minute starts in working hours over the next `days` working days, from tomorrow."""
    out, d, n = [], (now.astimezone(TZ) + timedelta(days=1)).date(), 0
    while n < days:
        if d.weekday() < 5:
            n += 1
            for h in range(start_h, end_h):
                for m in (0, 30):
                    out.append(datetime(d.year, d.month, d.day, h, m, tzinfo=TZ))
        d += timedelta(days=1)
    return out


def free_slots(candidates: list[datetime], busy: list[tuple[str, str]], exclude: list[str],
               want: int = 3) -> list[str]:
    """Pick up to `want` free slots, spread over different days, never one already offered/rejected."""
    bs = [(datetime.fromisoformat(a.replace("Z", "+00:00")), datetime.fromisoformat(b.replace("Z", "+00:00")))
          for a, b in busy]
    excluded = {datetime.fromisoformat(x) for x in exclude}
    picked, days = [], set()
    for s in candidates:
        e = s + timedelta(minutes=30)
        if s in excluded or any(s < b and e > a for a, b in bs):
            continue
        if s.date() in days:
            continue
        picked.append(s.isoformat())
        days.add(s.date())
        if len(picked) >= want:
            break
    return picked


def fmt_slot(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(TZ).strftime("%a %d %b, %I:%M %p IST").replace(" 0", " ")


@dataclass
class Toolbox:
    """Real implementations are wired in src/agent/runner.py; tests pass fakes."""
    send: Callable[..., dict]                     # (subject, body, thread_id, reply_to) -> {id, threadId, rfc_id}
    replies: Callable[[str], list[dict]]          # thread_id -> [{id, rfc_id, at, text}]
    busy: Callable[[datetime, datetime], list]    # (start, end) -> [(start, end)]
    book: Callable[..., str]                      # (start, end, summary, description) -> event_id
    crm: Callable[..., dict]                      # (domain, outcome_line, lifecycle) -> {company_id, updated}
    classify: Callable[[str, list[str]], dict]    # (reply_text, times_proposed) -> {intent, slot_index, proposed_time, reason}
    now: Callable[[], datetime] = field(default=lambda: datetime.now(TZ))
