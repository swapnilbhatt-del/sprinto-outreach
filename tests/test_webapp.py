"""Discovery and the local web app: exact-name domain matching, quote-checked candidates, seller
matching, the people list, and the localhost-only guard."""
import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from src.discover import discover, match_domain
from src.research.fetch import FetchResult
from src.webapp import account_summary, match_seller

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


def _serve(handler_cls, icp):
    import src.server as server
    server._ICP = icp
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _req(port, method, path, body=None, headers=None):
    c = HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Host": f"127.0.0.1:{port}", "Content-Type": "application/json", **(headers or {})}
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    return r.status, json.loads(r.read() or b"{}"), r.getheader("Set-Cookie")


def test_local_server_answers_only_on_localhost(icp, monkeypatch):
    from src.webapp import LocalHandler
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    srv, port = _serve(LocalHandler, icp)
    try:
        assert _req(port, "GET", "/api/me", headers={"Host": "evil.example"})[0] == 403
        status, body, _ = _req(port, "GET", "/api/seller?name=Vanta")
        assert status == 404 and "isn't set up yet" in body["error"]
    finally:
        srv.shutdown()


def test_deployed_api_needs_login_and_cron_needs_its_secret(icp, monkeypatch, tmp_path):
    from src.server import Handler
    monkeypatch.setenv("APP_PASSWORD", "pw-123")
    monkeypatch.setenv("CRON_SECRET", "cron-xyz")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    import src.server as server
    monkeypatch.setattr(server, "data", lambda: __import__("src.appdata", fromlist=["AppData"]).AppData(tmp_path / "s.sqlite"))
    srv, port = _serve(Handler, icp)
    try:
        assert _req(port, "GET", "/api/accounts")[0] == 401
        assert _req(port, "POST", "/api/login", {"password": "nope"})[0] == 401
        status, _, cookie = _req(port, "POST", "/api/login", {"password": "pw-123"})
        assert status == 200 and "HttpOnly" in cookie
        session = cookie.split(";")[0]
        assert _req(port, "GET", "/api/accounts", headers={"Cookie": session})[0] == 200
        assert _req(port, "POST", "/api/agent/tick", {}, headers={"Authorization": "Bearer wrong"})[0] == 401
        status, body, _ = _req(port, "POST", "/api/agent/tick", {}, headers={"Authorization": "Bearer cron-xyz"})
        assert status == 200 and body == {"ticked": []}
    finally:
        srv.shutdown()


def test_vercel_rewrite_path_is_restored():
    from src.server import original_url
    assert original_url("/api/index?__path=/api/seller&name=Sprinto").path == "/api/seller"
    assert original_url("/api/index?__path=/api/seller&name=Sprinto").query == "name=Sprinto"
    assert original_url("/api/index?__path=/").path == "/"
    assert original_url("/api/me").path == "/api/me"                     # local server: unchanged


def test_open_access_skips_the_password_but_not_the_cron_secret(icp, monkeypatch, tmp_path):
    from src.server import Handler
    monkeypatch.setenv("APP_PASSWORD", "pw-123")
    monkeypatch.setenv("OPEN_ACCESS", "1")
    monkeypatch.setenv("CRON_SECRET", "cron-xyz")
    import src.server as server
    monkeypatch.setattr(server, "data", lambda: __import__("src.appdata", fromlist=["AppData"]).AppData(tmp_path / "o.sqlite"))
    srv, port = _serve(Handler, icp)
    try:
        assert _req(port, "GET", "/api/me")[1] == {"authed": True, "password_required": False}
        assert _req(port, "GET", "/api/accounts")[0] == 200
    finally:
        srv.shutdown()
