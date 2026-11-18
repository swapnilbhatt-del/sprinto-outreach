"""The loop must stop at its caps even when a gate is never answered, and only take whitelisted actions."""
import pytest

from src.agent_loop import AgentLoop, CapError
from src.llm import CallBudget

from .conftest import TODAY, FakeFetcher, FakeLLM


@pytest.mark.parametrize("default", ["challenge", "not_found", "error", "robots_blocked"])
def test_loop_stops_at_caps_when_gates_never_answered(icp, default):
    llm, fetcher = FakeLLM(), FakeFetcher(default=default)
    res = AgentLoop(icp, "acme.com", fetcher, llm, today=TODAY).run()
    cfg = icp["agent_loop"]
    assert res.passes <= cfg["max_passes"]
    assert res.usage["pages"] <= cfg["max_pages_per_company"]
    assert res.usage["free_api_calls"] <= cfg["max_free_api_calls_per_company"]
    assert len(llm.calls) <= cfg["max_llm_calls_per_company"] - cfg["llm_calls_reserved_for_draft"]
    assert res.exit_reason in ("max_passes", "llm_cap", "no_allowed_actions", "planner_stop")
    assert any(v["status"] == "unknown" for v in res.gates.values())


def test_tight_caps_are_respected(icp):
    icp["agent_loop"].update(max_passes=2, max_pages_per_company=1, max_llm_calls_per_company=3,
                             max_free_api_calls_per_company=2)
    llm, fetcher = FakeLLM(), FakeFetcher(default="ok")
    res = AgentLoop(icp, "acme.com", fetcher, llm, today=TODAY).run()
    assert res.passes <= 2
    assert res.usage["pages"] <= 1
    assert res.usage["free_api_calls"] <= 2
    assert len(llm.calls) <= 3 - icp["agent_loop"]["llm_calls_reserved_for_draft"]


@pytest.mark.parametrize("cap", ["max_passes", "max_pages_per_company", "max_llm_calls_per_company"])
def test_loop_refuses_to_run_without_caps(icp, cap):
    del icp["agent_loop"][cap]
    with pytest.raises(CapError):
        AgentLoop(icp, "acme.com", FakeFetcher(), FakeLLM(), today=TODAY)


def test_planner_cannot_invent_actions_or_urls(icp):
    def planner(user, schema):
        return {"reasoning": "try something new", "stop": False,
                "actions": [{"action": "scrape_https://evil.example/", "gate": "G1", "why": "x"},
                            {"action": "fetch_linkedin", "gate": "G3", "why": "x"}]}
    llm, fetcher = FakeLLM({"planner": planner}), FakeFetcher()
    res = AgentLoop(icp, "acme.com", fetcher, llm, today=TODAY).run()
    taken = {a["action"] for a in res.actions}
    assert taken <= set(icp["actions"]) | {f"news:{q}" for g in icp["gates"].values()
                                           for q in (g.get("news_queries") or {})}
    assert not any("evil.example" in u or "linkedin" in u for u in fetcher.urls)
    assert all(p["decided_by"] in ("seed", "fallback", "planner") for p in res.planner)


def test_no_scraping_before_free_sources(icp):
    llm, fetcher = FakeLLM(), FakeFetcher()
    res = AgentLoop(icp, "acme.com", fetcher, llm, today=TODAY).run()
    first = res.planner[0]
    assert first["decided_by"] == "seed"
    assert all(icp["actions"].get(a["action"], {"tier": "free"})["tier"] == "free" for a in first["actions"])


def test_every_planner_decision_is_logged_with_reasoning(icp):
    res = AgentLoop(icp, "acme.com", FakeFetcher(), FakeLLM(), today=TODAY).run()
    assert res.planner and all(p["reasoning"] for p in res.planner)


def test_shared_budget_is_never_exceeded(icp):
    budget = CallBudget(4)
    llm = FakeLLM()
    AgentLoop(icp, "acme.com", FakeFetcher(default="ok"), llm, llm_budget=budget, today=TODAY).run()
    assert budget.used <= 4 and len(llm.calls) == budget.used


def test_caps_hold_when_every_page_is_reachable_but_empty(icp):
    # All sources answer but pages are near-empty shells: nothing counts as checked, nothing is invented.
    llm, fetcher = FakeLLM(), FakeFetcher(default="ok")
    res = AgentLoop(icp, "acme.com", fetcher, llm, today=TODAY).run()
    assert res.usage["pages"] <= icp["agent_loop"]["max_pages_per_company"]
    assert len(llm.calls) <= icp["agent_loop"]["max_llm_calls_per_company"]
    assert res.gates["G1"]["status"] == "unknown"           # never invents an answer


def test_link_discovery_prefers_exact_page_names(icp):
    loop = AgentLoop(icp, "acme.com", FakeFetcher(), FakeLLM(), today=TODAY)
    loop.homepage_links = [("https://www.acme.com/blog/company-news/ai-launch", ""),
                           ("https://www.acme.com/trust/compliance/resources/fedramp", ""),
                           ("https://www.acme.com/trust/ai", ""), ("https://www.acme.com/company/careers", ""),
                           ("https://www.acme.com/trust", ""), ("https://evil.example/trust", "")]
    assert loop._candidates("scrape_trust")[0][0] == "https://www.acme.com/trust"
    assert loop._candidates("scrape_careers")[0][0] == "https://www.acme.com/company/careers"
    assert all("blog" not in u for u, _ in loop._candidates("scrape_about"))
    assert all("evil" not in u for a in icp["actions"] if a.startswith("scrape")
               for u, _ in loop._candidates(a))


def test_one_guess_when_homepage_was_read_but_does_not_link_the_page(icp):
    loop = AgentLoop(icp, "acme.com", FakeFetcher(), FakeLLM(), today=TODAY)
    assert len(loop._candidates("scrape_trust")) == 2          # homepage not read yet: two guesses
    loop.homepage_read, loop.homepage_links = True, [("https://www.acme.com/pricing", "")]
    assert [u for u, _ in loop._candidates("scrape_trust")] == ["https://acme.com/trust"]
    loop.homepage_links.append(("https://www.acme.com/trust-center", ""))
    assert loop._candidates("scrape_trust")[0][0] == "https://www.acme.com/trust-center"   # linked page wins
    assert len(loop._candidates("scrape_trust")) == 2


def test_homepage_is_read_before_other_scrapes(icp):
    loop = AgentLoop(icp, "acme.com", FakeFetcher(), FakeLLM(), today=TODAY)
    out = loop._homepage_first([{"action": "scrape_trust", "gate": "G5", "why": "x"}])
    assert [c["action"] for c in out] == ["scrape_homepage", "scrape_trust"]
    out = loop._homepage_first([{"action": "scrape_trust", "gate": "G5", "why": "x"},
                                {"action": "scrape_homepage", "gate": "G1", "why": "y"}])
    assert [c["action"] for c in out] == ["scrape_homepage", "scrape_trust"]
    loop.done["scrape_homepage"] = {"outcome": "ok"}
    assert [c["action"] for c in loop._homepage_first([{"action": "scrape_trust", "gate": "G5", "why": "x"}])] == ["scrape_trust"]



def test_javascript_shell_page_does_not_count_as_checked(icp):
    from src.qualify import evaluate_gates
    shell = "<html><head><title>Trust Vault</title></head><body><div id=root></div></body></html>"
    fetcher = FakeFetcher(pages={"https://acme.com/": "<html><body>" + "<p>Acme sells software to businesses.</p>" * 10 + "</body></html>",
                                 "https://acme.com/trust": shell, "https://trust.acme.com/": shell})
    llm = FakeLLM()
    loop = AgentLoop(icp, "acme.com", fetcher, llm, today=TODAY)
    loop._scrape(2, "scrape_trust")
    assert loop.done["scrape_trust"]["outcome"] == "empty_page"
    assert not any("Trust Vault" in user for _, user, _ in llm.calls)   # no LLM call spent on it
    loop.done["scrape_security"] = {"outcome": "not_found"}
    loop.done["scrape_homepage"] = {"outcome": "ok"}
    assert evaluate_gates(icp, [], loop.done, TODAY)["G5"]["status"] == "unknown"


def test_vendor_headcount_waits_for_free_sources_and_g1_g5_then_runs_before_scraping(icp):
    calls = []

    def lookup(domain):
        calls.append(domain)
        return [], "no_size"

    loop = AgentLoop(icp, "acme.com", FakeFetcher(), FakeLLM(), today=TODAY, company_lookup=lookup)
    gates = {g: {"status": "unknown"} for g in icp["gate_order"]}
    vendor = lambda: [a for a in loop._allowed_actions(gates) if a["tier"] == "vendor"]
    assert vendor() == []                                   # free G3 sources not run yet
    for a in icp["gates"]["G3"]["sources"]["free"]:
        loop.done[a] = {"outcome": "ok"}
    assert vendor() == []                                   # G1 and G5 still unknown: credits held
    g3_scrapes = [a["action"] for a in loop._allowed_actions(gates) if "G3" in a["serves"] and a["tier"] == "scrape"]
    assert g3_scrapes == []                                 # and G3 scraping still waits for the vendor
    gates["G1"]["status"] = "pass"
    assert vendor() == []                                   # G5 still unknown
    gates["G5"]["status"] = "pass"
    allowed = loop._allowed_actions(gates)
    assert [a["action"] for a in allowed if a["tier"] == "vendor"] == ["hunter_company_lookup"]
    order = [c["action"] for c in loop._vendor_first([{"action": "scrape_about", "gate": "G3", "why": "t"}], allowed)]
    assert order[0] == "hunter_company_lookup"
    # In a run where G1/G5 never pass, the lookup never spends a credit.
    AgentLoop(icp, "acme.com", FakeFetcher(), FakeLLM(), today=TODAY, company_lookup=lookup).run()
    assert calls == []



def test_held_vendor_lookup_still_runs_after_gate_pass_budget_is_spent(icp):
    loop = AgentLoop(icp, "acme.com", FakeFetcher(), FakeLLM(), today=TODAY, company_lookup=lambda d: ([], "no_size"))
    for a in icp["gates"]["G3"]["sources"]["free"]:
        loop.done[a] = {"outcome": "ok"}
    loop.gate_passes["G3"] = icp["gates"]["G3"]["max_passes"]          # G3's own passes are used up
    gates = {g: {"status": "pass"} for g in icp["gate_order"]}
    gates["G3"]["status"] = "unknown"
    assert [a["action"] for a in loop._allowed_actions(gates) if a["tier"] == "vendor"] == ["hunter_company_lookup"]


PLIANT_HOME = ("<html><body><main><h1>Pliant: card issuing for B2B payments</h1>"
               "<p>Corporate cards and payment infrastructure for finance teams and software platforms.</p>"
               "<p>Issue cards, automate expenses, reconcile in real time, and connect your accounting stack.</p></main>"
               "<footer><a href='/en/document/certificate/iso-27001'>ISO Certificate 27001-2022.</a>"
               "<a href='/en/cards-as-a-service/features/compliance-security'>Compliance & security</a>"
               "<a href='https://other.example/iso-27001'>Partner ISO page</a>"
               "<a href='/en/careers'>Careers</a></footer></body></html>")


def test_footer_certificate_link_text_survives_cleaning():
    from src.research.clean import attestation_links
    lines = attestation_links(PLIANT_HOME, "https://www.getpliant.com/en", "getpliant.com")
    assert lines[0].startswith("ISO Certificate 27001-2022. (https://www.getpliant.com/en/document/certificate/iso-27001)")
    assert not any("other.example" in l for l in lines) and not any("Careers" in l for l in lines)


def test_security_check_follows_certificate_link_first(icp):
    from src.research.clean import extract_links
    loop = AgentLoop(icp, "getpliant.com", FakeFetcher(), FakeLLM(), today=TODAY)
    loop.homepage_links = extract_links(PLIANT_HOME, "https://www.getpliant.com/en")
    loop.homepage_read = True
    urls = [u for u, _ in loop._candidates("scrape_security")]
    assert urls[0] == "https://www.getpliant.com/en/document/certificate/iso-27001"
    assert "https://www.getpliant.com/en/cards-as-a-service/features/compliance-security" in urls


def test_homepage_text_given_to_extraction_includes_certificate_link(icp):
    seen = []

    def extract(user, schema):
        seen.append(user)
        from tests.conftest import unknown_extraction
        return unknown_extraction()

    class Site(FakeFetcher):
        def get(self, url, allowed_hosts=()):
            from src.research.fetch import FetchResult
            return FetchResult(url, "ok", 200, text=PLIANT_HOME, final_url="https://www.getpliant.com/en")

    loop = AgentLoop(icp, "getpliant.com", Site(), FakeLLM({"extract": extract}), today=TODAY)
    loop._scrape(2, "scrape_homepage")
    assert any("ISO Certificate 27001-2022." in u for u in seen)
