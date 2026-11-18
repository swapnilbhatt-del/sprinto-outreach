"""L6 reply agent: every branch of the graph, the retry table, and persistence between ticks."""
from datetime import datetime, timedelta

import pytest

from src.agent.graph import build_graph, new_state
from src.agent.runner import start_thread, tick
from src.agent.tools import (RETRY_TABLE, TOOL_DESCRIPTIONS, TZ, PermanentToolError, Toolbox, TransientToolError,
                             candidate_slots, free_slots, run_tool)
from src.appdata import AppData

NOW = datetime(2026, 11, 18, 9, 0, tzinfo=TZ)          # a Wednesday


class Fake:
    """Scripted Gmail/Calendar/CRM/classifier. `script` maps reply text -> classification."""
    def __init__(self, script=None, busy=None, crm_error=None, cal_error=None):
        self.sent, self.booked, self.crm_calls, self.inbox = [], [], [], []
        self.script, self._busy, self.crm_error, self.cal_error = script or {}, busy or [], crm_error, cal_error
        self.clock = NOW

    def toolbox(self):
        def send(subject, body, thread_id, reply_to):
            self.sent.append({"subject": subject, "body": body, "thread_id": thread_id})
            return {"id": f"m{len(self.sent)}", "threadId": thread_id or "t1", "rfc_id": f"<m{len(self.sent)}@x>"}

        def busy(a, b):
            if self.cal_error:
                raise self.cal_error
            return self._busy

        def crm(domain, line, stage):
            if self.crm_error:
                raise self.crm_error
            self.crm_calls.append((domain, line, stage))
            return {"company_id": "1", "updated": True}

        return Toolbox(send=send, replies=lambda tid: list(self.inbox), busy=busy,
                       book=lambda s, e, summary, d: self.booked.append(s) or "ev1", crm=crm,
                       classify=lambda text, offered: self.script[text], now=lambda: self.clock)

    def reply(self, text):
        self.inbox.append({"id": f"r{len(self.inbox)}", "rfc_id": f"<r{len(self.inbox)}@x>", "at": "", "text": text})


def run(fake, state, hours=48):
    out = build_graph(fake.toolbox(), no_reply_hours=hours).invoke({**state, "log": [], "route": ""})
    return out


def fresh():
    return dict(new_state("acme.com", "Acme", {"first_name": "Cy", "position": "CEO"},
                          {"threadId": "t1", "rfc_id": "<m0@x>"}, NOW))


def test_every_tool_description_has_the_four_parts():
    for name, d in TOOL_DESCRIPTIONS.items():
        low = d.lower()
        assert "use " in low and ("do not use" in low or "never" in low) and "returns" in low, name
        assert name in RETRY_TABLE


def test_waits_quietly_then_escalates_after_silence():
    f = Fake()
    s = run(f, fresh())
    assert s["route"] == "wait" and s["thread_status"] == "active" and not f.sent
    f.clock = NOW + timedelta(hours=49)
    s = run(f, s)
    assert s["thread_status"] == "escalated" and "48 hours" in s["escalation_reason"]
    assert f.sent[0]["subject"].startswith("[Escalation]") and f.crm_calls        # escalate -> update_crm


def test_interested_then_confirm_books_and_logs():
    f = Fake(script={"sounds good": {"intent": "interested", "slot_index": -1, "proposed_time": "", "reason": ""},
                     "the second one": {"intent": "confirm", "slot_index": 1, "proposed_time": "", "reason": ""}})
    f.reply("sounds good")
    s = run(f, fresh())
    assert s["reply_status"] == "interested" and s["calendar_checked"] and len(s["times_proposed"]) == 3
    assert s["meeting_status"] == "pending_confirmation" and f.sent[-1]["thread_id"] == "t1"
    f.reply("the second one")
    s = run(f, s)
    assert s["meeting_status"] == "confirmed" and f.booked == [datetime.fromisoformat(s["times_proposed"][1])]
    assert s["crm_logged"] and s["thread_status"] == "done" and f.crm_calls[0][2] == "opportunity"


def test_counter_proposal_rechecks_calendar_then_escalates_at_round_two():
    t = (NOW + timedelta(days=6)).replace(hour=15, minute=0).isoformat()
    f = Fake(script={"interested": {"intent": "interested", "slot_index": -1, "proposed_time": "", "reason": ""},
                     "how about 3pm": {"intent": "counter_propose", "slot_index": -1, "proposed_time": t, "reason": ""},
                     "no, friday": {"intent": "counter_propose", "slot_index": -1, "proposed_time": "", "reason": ""}})
    f.reply("interested")
    s = run(f, fresh())
    first_offer = list(s["times_proposed"])
    f.reply("how about 3pm")
    s = run(f, s)
    assert s["reply_round"] == 1 and s["times_proposed"] == [t]          # their time was free: offered back
    assert not set(first_offer) & set(s["times_proposed"])
    f.reply("no, friday")
    s = run(f, s)
    assert s["reply_round"] == 2 and s["thread_status"] == "escalated" and "round 2" in s["escalation_reason"]


def test_not_interested_logs_and_ends():
    f = Fake(script={"no thanks": {"intent": "not_interested", "slot_index": -1, "proposed_time": "", "reason": ""}})
    f.reply("no thanks")
    s = run(f, fresh())
    assert s["reply_status"] == "not_interested" and s["thread_status"] == "done" and s["crm_logged"]
    assert not f.sent                                                        # nothing else is emailed


def test_confirm_without_a_valid_slot_is_escalated_not_guessed():
    f = Fake(script={"yes": {"intent": "confirm", "slot_index": 7, "proposed_time": "", "reason": ""}})
    f.reply("yes")
    s = run(f, fresh())
    assert s["reply_status"] == "unclear" and s["thread_status"] == "escalated" and not f.booked


def test_retry_table_calendar_timeouts_escalate_and_crm_down_is_handed_to_a_human():
    f = Fake(script={"ok": {"intent": "interested", "slot_index": -1, "proposed_time": "", "reason": ""}},
             cal_error=TransientToolError("timeout"))
    f.reply("ok")
    import time                                     # no real back-off waits in the test
    real_sleep, time.sleep = time.sleep, lambda *_: None
    try:
        s = run(f, fresh())
    finally:
        time.sleep = real_sleep
    assert s["thread_status"] == "escalated" and "check_calendar" in s["escalation_reason"]
    assert sum(1 for e in s["log"] if e["outcome"] == "transient_error") == 3   # tried 3 times first
    f2 = Fake(script={"no": {"intent": "not_interested", "slot_index": -1, "proposed_time": "", "reason": ""}},
              crm_error=PermanentToolError("HUBSPOT_TOKEN is not set"))
    f2.reply("no")
    s2 = run(f2, fresh())
    assert s2["thread_status"] == "escalated" and not s2["crm_logged"] and "CRM not updated" in s2["escalation_reason"]
    assert [m["subject"] for m in f2.sent if m["subject"].startswith("[Escalation]")]      # the human is told
    f3 = Fake(script={"yes": {"intent": "confirm", "slot_index": 0, "proposed_time": "", "reason": ""}},
              crm_error=PermanentToolError("HUBSPOT_TOKEN is not set"))
    f3.reply("yes")
    s3 = run(f3, {**fresh(), "times_proposed": ["2026-11-23T15:00:00+05:30"]})
    esc = [m for m in f3.sent if m["subject"].startswith("[Escalation]")]
    assert s3["meeting_status"] == "confirmed" and f3.booked and len(esc) == 1          # booked, told once, no loop
    assert "log it by hand: Meeting booked" in esc[0]["body"]


def test_run_tool_follows_the_table():
    calls = []

    def flaky():
        calls.append(1)
        raise TransientToolError("x")
    r = run_tool("send_email", flaky, sleep=lambda *_: None)
    assert (r.ok, r.action, len(calls)) == (False, "skip", 3)
    r = run_tool("book_meeting", lambda: (_ for _ in ()).throw(PermanentToolError("bounced")), sleep=lambda *_: None)
    assert (r.ok, r.action) == (False, "abort")


def test_slots_skip_busy_and_already_offered_times():
    cands = candidate_slots(NOW, days=2)
    first = cands[0]
    busy = [(first.isoformat(), (first + timedelta(minutes=30)).isoformat())]
    picks = free_slots(cands, busy, exclude=[], want=2)
    assert first.isoformat() not in picks and len(picks) == 2
    assert len({datetime.fromisoformat(p).date() for p in picks}) == 2                  # spread over days
    assert not set(free_slots(cands, busy, exclude=picks, want=2)) & set(picks)


def test_start_and_tick_persist_state_between_runs(tmp_path):
    data = AppData(tmp_path / "a.sqlite")
    f = Fake(script={"interested": {"intent": "interested", "slot_index": -1, "proposed_time": "", "reason": ""}})
    trail = {"domain": "acme.com", "company": {"name": "Acme"}, "gates": {"G1": {"status": "pass"}},
             "contact": {"chosen": {"first_name": "Cy", "position": "CEO", "email": "cy@acme.com"}, "why": "w"},
             "email": {"draft": {"subject": "Hi", "body": "Hi Cy", "pain_points": [], "hook_fact_id": 1,
                                 "hook_quote": "q", "hook_url": "u", "reasoning": "r"}}}
    assert start_thread(data, trail, tb=f.toolbox(), lessons_used=[3])["ok"]
    assert start_thread(data, trail, tb=f.toolbox())["ok"] is False                     # one thread per account
    assert tick(data, tb=f.toolbox())[0]["reply_status"] == "no_reply"
    f.reply("interested")
    out = tick(data, tb=f.toolbox())
    st = data.get_thread("acme.com")
    assert out[0]["meeting_status"] == "pending_confirmation" and st["state"]["calendar_checked"]
    assert st["state"]["lessons_used"] == [3]
    fb = data.feedback_rows()
    assert fb[0]["reply_status"] == "interested" and fb[0]["lessons_used"] == [3]       # L5 sees the outcome
    assert any(e["node"] == "propose_time" for e in data.events("acme.com"))
