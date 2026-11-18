"""L5 feedback loop: lessons need evidence and approval, and approved lessons reach the drafter."""
from src.appdata import AppData
from src.draft_email import draft
from src.feedback import approved_lessons, propose_lessons
from src.llm import CallBudget

from .conftest import FakeLLM


def trail(domain, body):
    return {"domain": domain, "email": {"draft": {"subject": "S", "body": body}}}


def test_needs_two_signals_and_drops_unsupported_lessons(tmp_path):
    data = AppData(tmp_path / "f.sqlite")
    data.save_trail(trail("a.com", "Hi Ana,\nLong intro about compliance.\nWould a 15-minute call work?"))
    data.save_trail(trail("b.com", "Hi Ben,\nShort one.\nWould a 15-minute call work?"))
    f1 = data.add_feedback("a.com", 2, "Hi Ana,\nShort one.\nWould a 15-minute call work?", "too long", None, [])
    llm = FakeLLM({"lessons": lambda u, s: {"lessons": [
        {"text": "Keep the body to three short sentences.", "rationale": "edit cut the intro", "evidence": [f1]},
        {"text": "Mention their SOC 2 audit next week.", "rationale": "x", "evidence": [999]},
        {"text": " ".join(["word"] * 40), "rationale": "x", "evidence": [f1]}]}})
    assert propose_lessons(data, llm)["ok"] is False                     # one signal is not a pattern
    data.add_feedback("b.com", None, None, None, "interested", [])
    out = propose_lessons(data, llm)
    assert out["ok"] and len(out["added"]) == 1
    assert {d["reason"] for d in out["dropped"]} == {"cites no real feedback", "longer than 30 words"}
    assert data.lessons("pending")[0]["evidence"] == [f1]
    assert approved_lessons(data) == []                                  # nothing is used until approved
    data.decide_lesson(out["added"][0], "approved")
    assert approved_lessons(data)[0]["text"] == "Keep the body to three short sentences."
    again = propose_lessons(data, llm)
    assert "duplicate" in {d["reason"] for d in again["dropped"]}


def test_approved_lessons_reach_the_prompt_and_are_recorded(icp):
    seen = []

    def write(user, schema):
        return {"hook_fact_id": 1, "subject": "Hi", "cta": "Would a 15-minute call work?",
                "body": "Hi Cy,\n\nSaw the Series B.\n\nWould a 15-minute call work?\n\n— Sprinto team",
                "pain_points": [{"pain": "a", "fact_id": 1, "why_for_role": "x"}, {"pain": "b", "fact_id": 1, "why_for_role": "y"}],
                "reasoning": "r"}

    class Spy(FakeLLM):
        def call_json(self, purpose, system, user, schema, budget, max_tokens=4000):
            seen.append(system)
            return super().call_json(purpose, system, user, schema, budget, max_tokens)

    facts = [{"id": 1, "key": "funding_round", "value": {"value": "series_b"}, "quote": "raised a Series B",
              "source_url": "https://n.example/x", "date": "2026-10-01"}]
    status, d, _ = draft(Spy({"draft": write}), CallBudget(5), icp, "acme.com",
                         {"first_name": "Cy", "position": "CEO"}, facts, "why", lessons=[{"id": 7, "text": "Be brief."}])
    assert status == "drafted" and d["lessons_used"] == [7]
    assert "Be brief." in seen[0] and "approved by the Growth Lead" in seen[0]
