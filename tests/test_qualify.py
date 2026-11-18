"""Gate logic: one case per status (go, no_go, needs_review) plus short-circuit."""
from src.agent_loop import AgentLoop
from src.qualify import decide, evaluate_gates

from .conftest import TODAY, FakeFetcher, FakeLLM, unknown_extraction


def fact(i, key, value, quote="q", date="2026-10-01", url="https://x.com/p", st="page"):
    return {"id": i, "key": key, "value": value, "quote": quote, "source_url": url, "source_type": st, "date": date}


GOOD_FACTS = [
    fact(1, "is_software_product", {"value": "yes"}),
    fact(2, "sells_to_businesses", {"value": "yes"}),
    fact(3, "funding_round", {"value": "series_b"}, date="2025-06-01", st="news"),
    fact(4, "employee_count", {"value": "about 250", "min": 250, "max": 250}),
    fact(5, "compliance_job_titles", {"value": "GRC Analyst"}, st="job_board"),
]
CHECKED_G5 = {"scrape_homepage": {"outcome": "ok"}, "scrape_trust": {"outcome": "not_found"},
              "scrape_security": {"outcome": "not_found"}, "news:g5_soc2": {"outcome": "ok"}, "news:g5_iso": {"outcome": "ok"}}


def test_go_when_every_gate_passes(icp):
    gates = evaluate_gates(icp, GOOD_FACTS, CHECKED_G5, TODAY)
    assert {g: v["status"] for g, v in gates.items()} == {g: "pass" for g in icp["gate_order"]}
    assert decide(icp, gates)[0] == "go"


def test_no_go_when_any_gate_fails_even_if_others_pass(icp):
    facts = GOOD_FACTS + [fact(6, "soc2_iso_status", {"value": "attested_type2_or_iso"},
                               quote="We are SOC 2 Type II certified")]
    status, failing, reason = decide(icp, evaluate_gates(icp, facts, CHECKED_G5, TODAY))
    assert (status, failing) == ("no_go", "G5")
    assert "SOC 2 Type II" in reason


def test_no_go_below_100_employees(icp):
    facts = [f for f in GOOD_FACTS if f["key"] != "employee_count"] + [
        fact(7, "employee_count", {"value": "40 people", "min": 40, "max": 40})]
    assert decide(icp, evaluate_gates(icp, facts, CHECKED_G5, TODAY))[:2] == ("no_go", "G3")


def test_needs_review_when_a_gate_is_unknown(icp):
    facts = [f for f in GOOD_FACTS if f["key"] != "funding_round"]
    gates = evaluate_gates(icp, facts, CHECKED_G5, TODAY)
    assert gates["G2"]["status"] == "unknown"
    status, failing, _ = decide(icp, gates)
    assert status == "needs_review" and failing is None


def test_straddling_headcount_is_unknown_not_pass(icp):
    facts = [f for f in GOOD_FACTS if f["key"] != "employee_count"] + [
        fact(8, "employee_count", {"value": "51-200", "min": 51, "max": 200})]
    assert evaluate_gates(icp, facts, CHECKED_G5, TODAY)["G3"]["status"] == "unknown"


def test_g5_needs_sources_checked_before_passing(icp):
    gates = evaluate_gates(icp, GOOD_FACTS, {}, TODAY)
    assert gates["G5"]["status"] == "unknown"     # absence of evidence is not a pass until sources were read


def test_short_circuit_stops_research_after_first_failure(icp):
    # The news digest says the company is SOC 2 Type II certified -> G5 fails in pass 1.
    def extract(user, schema):
        raw = unknown_extraction()
        if "News search results" in user:
            raw["soc2_iso_status"] = {"value": "attested_type2_or_iso",
                                      "quote": "Acme achieves SOC 2 Type II", "item": "1"}
        return raw
    pages = {}
    llm = FakeLLM({"extract": extract})

    class NewsFetcher(FakeFetcher):
        def get(self, url, allowed_hosts=()):
            self.urls.append(url)
            if "bing.com/news" in url:
                from src.research.fetch import FetchResult
                body = ("<rss><channel><item><title>Acme achieves SOC 2 Type II</title>"
                        "<link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;url=https%3a%2f%2fnews.example%2facme</link>"
                        "<pubDate>Mon, 05 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>")
                return FetchResult(url, "ok", 200, text=body, final_url=url)
            return super().get(url, allowed_hosts)

    fetcher = NewsFetcher(pages=pages)
    res = AgentLoop(icp, "acme.com", fetcher, llm, today=TODAY).run()
    assert res.exit_reason == "short_circuit:G5"
    assert res.passes == 1
    assert not any(c[0] == "planner" for c in llm.calls)              # no further planning
    assert not any(u.startswith("https://acme.com/") and "press" not in u and "news" not in u
                   for u in fetcher.urls)                              # no scraping after the failure
    assert decide(icp, res.gates)[:2] == ("no_go", "G5")


def test_acquisition_newer_than_last_round_fails_g2(icp):
    facts = GOOD_FACTS + [fact(7, "funding_round", {"value": "acquired"}, quote="Infotech acquires Acme",
                               date="2026-10-01", st="news")]
    status, failing, reason = decide(icp, evaluate_gates(icp, facts, CHECKED_G5, TODAY))
    assert (status, failing) == ("no_go", "G2")
    assert "acquired" in reason



def test_vendor_size_can_pass_g3_but_not_fail_it(icp):
    from src.qualify import gate_g3
    g = icp["gates"]["G3"]
    small = [fact(9, "employee_count", {"value": "11-50", "min": 11, "max": 50}, st="vendor")]
    status, reason, ev = gate_g3(g, small, {}, TODAY)
    assert status == "unknown" and "vendor estimate says below 100" in reason and ev == [9]
    big = [fact(9, "employee_count", {"value": "251-1K", "min": 251, "max": 1000}, st="vendor")]
    assert gate_g3(g, big, {}, TODAY)[0] == "pass"
    stated = [fact(9, "employee_count", {"value": "about 40", "min": 40, "max": 40}, st="page")]
    assert gate_g3(g, stated, {}, TODAY)[0] == "fail"           # a company's own statement still fails it


def test_g2_ignores_a_round_dated_only_by_page_fetch(icp):
    from src.qualify import gate_g2
    real = fact(1, "funding_round", {"value": "series_b"}, date="2025-11-23", st="news") | {"date_basis": "stated"}
    old = fact(2, "funding_round", {"value": "series_a"}, quote="2022 Series A Funding Raised $7.5 Million",
               date=TODAY.isoformat()) | {"date_basis": "fetched"}
    status, reason, ev = gate_g2(icp["gates"]["G2"], [real, old], {}, TODAY)
    assert status == "pass" and ev == [1] and "series_b" in reason


def test_g4_only_scores_it_never_rejects_or_blocks(icp):
    from src.qualify import weighted_score
    no_pressure = [f for f in GOOD_FACTS if f["key"] != "compliance_job_titles"]
    all_checked = dict(CHECKED_G5, job_board_lookup={"outcome": "not_found"}, scrape_careers={"outcome": "ok"},
                       **{"news:g4_ciso": {"outcome": "ok"}, "news:g4_head": {"outcome": "ok"}})
    gates = evaluate_gates(icp, no_pressure, all_checked, TODAY)
    assert gates["G4"]["status"] == "fail" and decide(icp, gates)[0] == "go"      # met nothing: still go
    gates = evaluate_gates(icp, no_pressure, CHECKED_G5, TODAY)
    assert gates["G4"]["status"] == "unknown" and decide(icp, gates)[0] == "go"   # unanswered: still go
    without, _ = weighted_score(icp, no_pressure, TODAY, gates=gates)
    with_g4, detail = weighted_score(icp, GOOD_FACTS, TODAY, gates=evaluate_gates(icp, GOOD_FACTS, CHECKED_G5, TODAY))
    assert with_g4 - without == icp["gates"]["G4"]["weight"] and detail["G4"]["points"] == 3
