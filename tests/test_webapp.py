"""Discovery and the local web app: exact-name domain matching, quote-checked candidates, seller
matching, the people list, and the localhost-only guard."""
import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from src.discover import discover, match_domain
from src.research.fetch import FetchResult
from src.webapp import Handler, account_summary, match_seller

from .conftest import FakeFetcher, FakeLLM

NEWS = ("<rss><channel>"
        "<item><title>Acme raises $20M Series B to automate billing</title><link>https://news.example/acme</link>"
        "<pubDate>Sat, 12 Sep 2026 09:00:00 GMT</pubDate></item>"
        "<item><title>Old Co raises Series A</title><link>https://news.example/old</link>"
        "<pubDate>Sat, 12 Sep 2026 09:00:00 GMT</pubDate></item>"
        "</channel></rss>")


class NewsFetcher(FakeFetcher):
    def get(self, url, allowed_hosts=()):
        return FetchResult(url, "ok", 200, text=NEWS, final_url=url)


class ClearbitHTTP:
    def get(self, url):
        q = url.split("query=")[-1].lower()
        body = {"acme": [{"name": "Acme Billing", "domain": "acmebilling.com"}, {"name": "Acme", "domain": "acme.com"}],
                "old+co": [{"name": "Old Company", "domain": "oldcompany.com"}],
                "zed": [{"name": "Zed", "domain": "zedpayroll.co.th"}]}.get(q, [])

        class R:
            status_code = 200

            def json(self):
                return body
        return R()

    def head(self, url):
        class R:
            pass
        r = R()
        r.url = url
        return r


def test_match_domain_needs_exact_name():
    sugg = [{"name": "Fleet X", "domain": "fleetx.app"}, {"name": "fleetx.io", "domain": "fleetx.io"}]
    assert match_domain("Fleetx", sugg) == ("fleetx.app", "exact")     # the domain itself is the name
    assert match_domain("Fleetx", [{"name": "FleetxTribe", "domain": "fleetxtribe.com"}]) == (None, "none")
    assert match_domain("Dreamteam", [{"name": "Dreamteam", "domain": "dreamteampayrolloutsource.co.th"}]) \
        == ("dreamteampayrolloutsource.co.th", "name_only")


def test_discover_keeps_only_quoted_raisers_with_domains(icp):
    def read(user, schema):
        return {"companies": [
            {"name": "Acme", "round": "Series B", "item": "1", "quote": "Acme raises $20M Series B"},
            {"name": "Old Co", "round": "Series A", "item": "2", "quote": "Old Co raises Series A"},
            {"name": "Ghost", "round": "Series C", "item": "1", "quote": "Ghost raises $90M"},
            {"name": "Zed", "round": "Series A", "item": "2", "quote": "Zed raises Series A"}]}
    icp["seller"]["discovery"]["queries"] = ["q1"]
    res = discover(NewsFetcher(), FakeLLM({"discover": read}), icp["seller"], "ua",
                   exclude={}, researched={"acme.com"}, http=ClearbitHTTP())
    by = {c["name"]: c for c in res["candidates"]}
    assert set(by) == {"Acme", "Old Co"} and {d["name"] for d in res["dropped"]} == {"Ghost", "Zed"}
    assert by["Acme"]["domain"] == "acme.com" and by["Acme"]["status"] == "researched"
    assert by["Old Co"]["domain"] is None and by["Old Co"]["status"] == "no_domain"


def test_seller_matching(icp):
    assert match_seller(icp, " sprinto ")["name"] == "Sprinto"
    assert match_seller(icp, "Vanta") is None and match_seller(icp, "") is None


def test_people_list_puts_primary_first_and_keeps_news_names():
    t = {"domain": "acme.com", "run_id": "20261117-100000", "final_status": "go", "final_reason": "ok",
         "gates": {"G1": {"status": "pass", "reason": "r"}},
         "contact": {"chosen": {"first_name": "Cy", "last_name": "Do", "email": "cy@acme.com", "position": "CEO"},
                     "why": "named in news",
                     "considered": [{"name": "Ana Rao", "position": "CTO", "email": "ana@acme.com", "rank": 5,
                                     "verification": "accept_all", "rejected": "email verification status 'accept_all' not accepted"},
                                    {"name": "Sales", "position": "Sales", "email": "sales@acme.com", "rank": None}],
                     "leader_fallback": {"tried": [{"name": "Ann Lee", "role": "co-founder", "finder": "not_found"}]}}}
    people = account_summary(t)["people"]
    assert [p["name"] for p in people] == ["Cy Do", "Ana Rao", "Ann Lee"]
    assert people[0]["role"] == "primary" and people[2]["email"] is None


def test_server_answers_only_on_localhost(icp):
    Handler.icp = icp
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        c = HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/api/accounts", headers={"Host": "evil.example"})
        assert c.getresponse().status == 403
        c = HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/api/seller?name=Vanta", headers={"Host": f"127.0.0.1:{port}"})
        r = c.getresponse()
        assert r.status == 404 and "isn't set up yet" in json.loads(r.read())["error"]
    finally:
        srv.shutdown()
