"""Full go path with fakes: research -> go -> Hunter -> draft -> dry-run send to OWN_EMAIL only."""
import re

from src.pipeline import process_domain
from src.research.fetch import FetchResult
from src.research.store import Store

from .conftest import TODAY, FakeFetcher, FakeLLM, unknown_extraction
from .test_send_and_contact import HUNTER, FakeHTTP

HOME = ("<html><head><title>Acme</title></head><body><h1>Acme is the billing platform for B2B SaaS teams.</h1>"
        "<p>Trusted by 300 finance teams. Book a demo.</p><p>We are 250 employees across 3 offices.</p>"
        "<p>We're hiring: GRC Analyst</p><p>Automate invoicing, revenue recognition and collections in one place, "
        "with integrations for your CRM, payment gateway and general ledger.</p></body></html>")
NEWS = ("<rss><channel><item><title>Acme raises $30M Series B to expand billing platform</title>"
        "<link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;url=https%3a%2f%2fnews.example%2facme-series-b</link>"
        "<pubDate>Sat, 12 Sep 2026 09:00:00 GMT</pubDate></item></channel></rss>")


class SiteFetcher(FakeFetcher):
    def get(self, url, allowed_hosts=()):
        self.urls.append(url)
        if "bing.com/news" in url:
            return FetchResult(url, "ok", 200, text=NEWS, final_url=url)
        if url == "https://acme.com/":
            return FetchResult(url, "ok", 200, text=HOME, final_url=url)
        return FetchResult(url, "not_found", 404)


def extract(user, schema):
    raw = unknown_extraction()
    if "News search results" in user:
        raw["funding_round"] = {"value": "series_b", "quote": "Acme raises $30M Series B", "item": "1", "date": "unknown"}
    elif "scrape_homepage" in user:
        raw["is_software_product"] = {"value": "yes", "quote": "the billing platform for B2B SaaS teams", "item": "none"}
        raw["sells_to_businesses"] = {"value": "yes", "quote": "Trusted by 300 finance teams", "item": "none"}
        raw["employee_count"] = {"value": "250 employees", "min": "250", "max": "250",
                                 "quote": "We are 250 employees", "item": "none", "date": "unknown"}
        raw["compliance_job_titles"] = [{"title": "GRC Analyst", "quote": "We're hiring: GRC Analyst", "item": "none"}]
    return raw


def draft(user, schema):
    fid = int(re.search(r"id (\d+) \[compliance_job_titles", user).group(1))
    other = int(re.search(r"id (\d+) \[funding_round", user).group(1))
    body = ("Hi Cy,\n\nSaw Acme is hiring a GRC Analyst. Teams that hire their first GRC person usually do it "
            "because enterprise deals are stuck on security questionnaires. Sprinto automates SOC 2 evidence "
            "collection so that hire starts on controls, not spreadsheets.\n\n"
            "Worth a 15-minute call next week?\n\n— Sprinto team")
    return {"hook_fact_id": fid, "subject": "Your GRC Analyst opening",
            "pain_points": [{"pain": "Security questionnaires slowing deals", "fact_id": fid, "why_for_role": "x"},
                            {"pain": "Audit prep after Series B", "fact_id": other, "why_for_role": "y"}],
            "body": body, "cta": "Worth a 15-minute call next week?", "reasoning": "Hiring signal is the freshest fact."}


def planner(user, schema):
    ids = schema["properties"]["actions"]["items"]["properties"]["action"]["enum"]
    want = [a for a in ("scrape_homepage", "scrape_trust", "scrape_security") if a in ids]
    return {"reasoning": "Homepage settles G1/G3; trust and security settle G5.", "stop": False,
            "actions": [{"action": a, "gate": "G5", "why": "t"} for a in want]}


def test_go_path_sends_only_to_own_email(icp, tmp_path, monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "test-key")
    monkeypatch.setenv("OWN_EMAIL", "me@example.com")
    store = Store(tmp_path / "t.sqlite", run_id="test")
    printed = []
    trail = process_domain("acme.com", icp, fetcher=SiteFetcher(), llm=FakeLLM({"extract": extract, "draft": draft, "planner": planner}),
                           store=store, dry_run=True, review=False, today=TODAY, hunter_http=FakeHTTP(HUNTER),
                           print_fn=printed.append)
    assert trail["final_status"] == "go", trail["final_reason"]
    assert all(g["status"] == "pass" for g in trail["gates"].values())
    assert trail["contact"]["chosen"]["email"] == "vpe@acme.com"
    assert trail["email"]["status"] == "drafted"
    assert trail["email"]["draft"]["hook_url"]                        # hook carries its source URL
    assert trail["send"] == {"mode": "dry_run", "to": "me@example.com", "message_id": None}
    assert "vpe@acme.com" not in printed[0].split("\n")[0]            # prospect is never the recipient
    assert "To: me@example.com" in printed[0]
    assert all(f.get("quote") and f.get("source_url") for f in trail["facts"])
    assert trail["llm_calls"] <= icp["agent_loop"]["max_llm_calls_per_company"]
    funding = next(f for f in trail["facts"] if f["key"] == "funding_round")
    assert funding["source_url"] == "https://news.example/acme-series-b"   # publisher URL, not Bing's redirect


def test_llm_outage_gives_retry_not_needs_review(icp, tmp_path, monkeypatch):
    from src.llm import LLMError
    monkeypatch.setenv("OWN_EMAIL", "me@example.com")

    def down(user, schema):
        raise LLMError("extract: every model unavailable (gemini-x:503)")

    trail = process_domain("acme.com", icp, fetcher=SiteFetcher(), llm=FakeLLM({"extract": down, "planner": planner}),
                           store=Store(tmp_path / "t.sqlite", run_id="test"), dry_run=True, review=False,
                           today=TODAY, hunter_http=FakeHTTP(HUNTER), print_fn=lambda *_: None)
    assert trail["final_status"] == "retry"
    assert "re-run this company later" in trail["final_reason"] and "send" not in trail


def test_llm_outages_only_counts_outage_failures():
    from src.pipeline import llm_outages
    acts = [{"action": "news:g2_raises", "outcome": "extract_failed", "note": "extract_failed: extract: every model unavailable (a:503)"},
            {"action": "scrape_about", "outcome": "extract_failed", "note": "extract_failed: extract: invalid JSON"},
            {"action": "scrape_homepage", "outcome": "ok"}]
    assert llm_outages(acts) == ["news:g2_raises"]


LEADER_NEWS = ("<rss><channel><item><title>Acme: People highlights</title>"
               "<link>https://news.example/acme-people</link><description>Acme was founded by Cy Do and Ann Lee. "
               "Cy Do is the CEO of Acme.</description>"
               "<pubDate>Sat, 12 Sep 2026 09:00:00 GMT</pubDate></item></channel></rss>")


class LeaderFetcher(SiteFetcher):
    def get(self, url, allowed_hosts=()):
        if "bing.com/news" in url and "founder" in url.lower():
            self.urls.append(url)
            return FetchResult(url, "ok", 200, text=LEADER_NEWS, final_url=url)
        return super().get(url, allowed_hosts)


class RouteHTTP:
    """Hunter fake: domain search finds only untitled addresses; Email Finder knows the named CEO."""
    def __init__(self):
        self.urls = []

    def get(self, url, params):
        self.urls.append(url)
        if "email-finder" in url:
            body = {"data": {"email": "cy@acme.com", "score": 96, "verification": {"status": "valid"}}} \
                if params.get("last_name") == "Do" else {"data": {"email": None}}
        elif "domain-search" in url:
            body = {"data": {"emails": [{"value": "info@acme.com", "first_name": None, "position": None,
                                         "verification": {"status": "valid"}}]}}
        else:
            body = {"data": {}}

        class R:
            status_code = 200
            text = ""
            headers = {}

            def json(self):
                return body
        return R()


def leaders(user, schema):
    return {"leaders": [
        {"name": "Cy Do", "role": "CEO", "item": "1", "quote": "Cy Do is the CEO of Acme."},
        {"name": "Ann Lee", "role": "co-founder", "item": "1", "quote": "Acme was founded by Cy Do and Ann Lee."},
        {"name": "Ghost Person", "role": "CEO", "item": "1", "quote": "Ghost Person is the CEO of Acme."}]}


def test_leader_from_news_plus_email_finder_gives_contact(icp, tmp_path, monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "test-key")
    monkeypatch.setenv("OWN_EMAIL", "me@example.com")
    http = RouteHTTP()
    trail = process_domain("acme.com", icp, fetcher=LeaderFetcher(),
                           llm=FakeLLM({"extract": extract, "draft": draft, "planner": planner, "leaders": leaders}),
                           store=Store(tmp_path / "t.sqlite", run_id="test"), dry_run=True, review=False,
                           today=TODAY, hunter_http=http, print_fn=lambda *_: None)
    assert trail["final_status"] == "go", trail["final_reason"]
    chosen = trail["contact"]["chosen"]
    assert chosen["email"] == "cy@acme.com" and chosen["matched_title"] == "CEO"
    lf = trail["contact"]["leader_fallback"]
    # The co-founder ranks first but Email Finder has no address for her, so the CEO is tried next.
    assert [(t["name"], t["finder"]) for t in lf["tried"]] == [("Ann Lee", "not_found"), ("Cy Do", "api")]
    assert {d["name"] for d in lf["dropped"]} == {"Ghost Person"}                       # quote not in the news item
    assert "news.example/acme-people" in trail["contact"]["why"]
    assert trail["send"]["to"] == "me@example.com"
