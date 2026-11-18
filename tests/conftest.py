import copy
from datetime import date

import pytest

from src.config import load_icp
from src.research.fetch import FetchResult

TODAY = date(2026, 11, 4)


@pytest.fixture
def icp():
    icp = copy.deepcopy(load_icp())
    icp["agent_loop"]["news_min_interval_seconds"] = 0      # no real sleeping in tests
    return icp


def unknown_extraction():
    f = lambda **extra: {"value": "unknown", "quote": "", "item": "none", **extra}
    return {
        "is_software_product": f(), "sells_to_businesses": f(),
        "funding_round": f(date="unknown"),
        "employee_count": f(min="unknown", max="unknown", date="unknown"),
        "soc2_iso_status": f(), "security_leader_change": f(role="", person="", date="unknown"),
        "compliance_job_titles": [], "enterprise_motion": f(), "regulated_vertical": f(), "eu_presence": f(),
    }


class FakeLLM:
    """Spends the budget like the real client; answers come from per-purpose handlers."""
    def __init__(self, handlers=None):
        self.handlers = handlers or {}
        self.calls = []
        self.on_call = None
        self.total_calls = 0

    def call_json(self, purpose, system, user, schema, budget, max_tokens=4000):
        budget.spend()
        self.total_calls += 1
        self.calls.append((purpose, user, schema))
        h = self.handlers.get(purpose)
        if h:
            return h(user, schema)
        if purpose == "extract":
            return unknown_extraction()
        if purpose == "planner":
            ids = schema["properties"]["actions"]["items"]["properties"]["action"]["enum"]
            gates = schema["properties"]["actions"]["items"]["properties"]["gate"]["enum"]
            return {"reasoning": "test", "stop": False,
                    "actions": [{"action": a, "gate": gates[0], "why": "t"} for a in ids[:3]]}
        raise AssertionError(f"unexpected purpose {purpose}")


class FakeFetcher:
    """Every URL answers with the same canned response unless overridden in `pages`."""
    def __init__(self, default="not_found", pages=None):
        self.default, self.pages, self.urls = default, pages or {}, []

    def get(self, url, allowed_hosts=()):
        self.urls.append(url)
        if url in self.pages:
            return FetchResult(url, "ok", 200, text=self.pages[url], final_url=url)
        if self.default == "ok":
            return FetchResult(url, "ok", 200, text="<html><body><p>Nothing useful here.</p></body></html>", final_url=url)
        return FetchResult(url, self.default, 404 if self.default == "not_found" else None)
