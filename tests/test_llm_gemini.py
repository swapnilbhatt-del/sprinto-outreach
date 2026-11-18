"""Gemini client: 503 falls back to the next model, 429 waits once, the budget is spent once per call."""
from types import SimpleNamespace

import pytest
from google.genai import errors

from src.llm import CallBudget, GeminiJSON, LLMError


def api_error(code):
    return errors.APIError(code, {"error": {"code": code, "message": f"HTTP {code}", "status": "X"}})


def ok(text='{"a": 1}'):
    return SimpleNamespace(text=text, candidates=[SimpleNamespace(finish_reason="STOP")],
                           usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=5))


class FakeClient:
    def __init__(self, script):
        self.script, self.calls = script, []
        self.models = self

    def generate_content(self, model, contents, config):
        self.calls.append(model)
        out = self.script[model].pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def make(script, models=("big", "lite")):
    g = GeminiJSON(models=list(models), sleep=lambda s: slept.append(s), clock=lambda: 1e9,
                   min_interval_seconds=0, overload_pause_seconds=0)
    g._client = FakeClient(script)
    return g


slept = []


def test_503_falls_back_to_next_model_and_spends_budget_once():
    g, budget, logs = make({"big": [api_error(503)], "lite": [ok()]}), CallBudget(5), []
    g.on_call = logs.append
    assert g.call_json("extract", "s", "u", {}, budget) == {"a": 1}
    assert g._client.calls == ["big", "lite"]
    assert budget.used == 1 and logs[0]["model"] == "lite" and logs[0]["attempts"] == ["big:503"]


def test_429_waits_then_retries_same_model():
    slept.clear()
    g = make({"big": [api_error(429), ok()], "lite": []})
    assert g.call_json("extract", "s", "u", {}, CallBudget(5)) == {"a": 1}
    assert g._client.calls == ["big", "big"] and 20.0 in slept


def test_all_models_down_raises_llm_error():
    g = make({"big": [api_error(503)], "lite": [api_error(503)]})
    with pytest.raises(LLMError, match="every model unavailable"):
        g.call_json("extract", "s", "u", {}, CallBudget(5))


def test_client_error_is_not_retried():
    g = make({"big": [api_error(400)], "lite": [ok()]})
    with pytest.raises(LLMError, match="HTTP 400"):
        g.call_json("extract", "s", "u", {}, CallBudget(5))
    assert g._client.calls == ["big"]


def test_budget_exhausted_makes_no_request():
    from src.llm import BudgetExceeded
    g = make({"big": [ok()], "lite": []})
    with pytest.raises(BudgetExceeded):
        g.call_json("extract", "s", "u", {}, CallBudget(0))
    assert g._client.calls == []


def test_dropped_connection_falls_back_to_next_model():
    import httpx
    g = make({"big": [httpx.RemoteProtocolError("Server disconnected")], "lite": [ok()]})
    assert g.call_json("extract", "s", "u", {}, CallBudget(5)) == {"a": 1}
    assert g._client.calls == ["big", "lite"]


def daily_quota_error():
    return errors.APIError(429, {"error": {"code": 429, "message": "per day limit", "status": "RESOURCE_EXHAUSTED",
                                           "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                                                        "retryDelay": "5755s"}]}})


def test_daily_quota_skips_model_for_rest_of_run():
    slept.clear()
    g = make({"big": [daily_quota_error()], "lite": [ok(), ok()]})
    g.call_json("extract", "s", "u", {}, CallBudget(5))
    g.call_json("extract", "s", "u", {}, CallBudget(5))
    assert g._client.calls == ["big", "lite", "lite"]          # big asked once, then skipped
    assert "big" in g.exhausted and 5755 not in slept           # never waits out a daily quota
