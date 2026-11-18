"""Bing News RSS client: throttling, 429 handling, publisher-URL unwrapping, parsing."""
from src.research.fetch import FetchResult
from src.research.news import Throttle, news_search, publisher_url

from .conftest import FakeFetcher

RSS = ("<rss xmlns:News=\"https://www.bing.com/news/search?q=x&amp;format=rss\"><channel><item>"
       "<title>Acme raises $20 million led by Lightspeed</title>"
       "<link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;aid=&amp;tid=1&amp;url=https%3a%2f%2fwww.livemint.com%2facme-raises&amp;c=2</link>"
       "<description>New Delhi: Acme &amp;amp; friends raised money.</description>"
       "<pubDate>Thu, 13 Jul 2023 04:13:00 GMT</pubDate><News:Source>Mint</News:Source>"
       "</item></channel></rss>")


class Clock:
    def __init__(self):
        self.t, self.slept = 0.0, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class Scripted(FakeFetcher):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)

    def get(self, url, allowed_hosts=()):
        self.urls.append(url)
        status, body = self.responses.pop(0)
        outcome = "ok" if status == 200 else "challenge"
        return FetchResult(url, outcome, status, text=body, final_url=url)


def test_throttle_spaces_calls():
    c = Clock()
    th = Throttle(2, sleep=c.sleep, clock=c.now)
    th.wait()
    c.t += 0.5
    th.wait()
    assert c.slept == [1.5]


def test_parses_item_and_unwraps_publisher_url():
    c = Clock()
    items, outcome = news_search(Scripted([(200, RSS)]), '"acme" raises', 5, Throttle(0, c.sleep, c.now))
    assert outcome == "ok" and len(items) == 1
    it = items[0]
    assert it.url == "https://www.livemint.com/acme-raises"
    assert it.date == "2023-07-13"
    assert "Acme raises $20 million led by Lightspeed" in it.text and "Mint" in it.text


def test_no_results_is_checked_not_unknown():
    c = Clock()
    assert news_search(Scripted([(200, "<rss><channel></channel></rss>")]), '"acme" CISO', 5,
                       Throttle(0, c.sleep, c.now)) == ([], "ok")


def test_http_429_is_retried_then_reported_as_rate_limited():
    c = Clock()
    f = Scripted([(429, ""), (429, "")])
    assert news_search(f, '"acme" CISO', 5, Throttle(2, c.sleep, c.now)) == ([], "rate_limited")
    assert len(f.urls) == 2


def test_non_bing_links_are_left_alone():
    assert publisher_url("https://example.com/a") == "https://example.com/a"


from src.agent_loop import AgentLoop  # noqa: E402
from src.research.news import resolve_company_name  # noqa: E402

from .conftest import TODAY, FakeLLM  # noqa: E402

CLEARBIT = '[{"name": "Hevo Data", "domain": "hevodata.com"}, {"name": "Hevo Data", "domain": "hevodata.io"}]'


class ClearbitFetcher(FakeFetcher):
    def __init__(self, body):
        super().__init__()
        self.body = body

    def get(self, url, allowed_hosts=()):
        self.urls.append(url)
        if "clearbit.com" in url:
            return FetchResult(url, "ok", 200, text=self.body, final_url=url)
        return super().get(url, allowed_hosts)


def test_input_file_name_wins_without_any_api_call():
    f = ClearbitFetcher(CLEARBIT)
    assert resolve_company_name(f, "hevodata.com", "Hevo", lambda: True) == ("Hevo", "input_file")
    assert f.urls == []


def test_clearbit_only_accepted_on_exact_domain_match():
    assert resolve_company_name(ClearbitFetcher(CLEARBIT), "hevodata.com", None, lambda: True) == ("Hevo Data", "clearbit")
    other = '[{"name": "Hevo Data", "domain": "hevodata.io"}]'
    assert resolve_company_name(ClearbitFetcher(other), "hevodata.com", None, lambda: True) == ("hevodata", "domain_stem")


def test_falls_back_to_stem_when_api_cap_reached():
    f = ClearbitFetcher(CLEARBIT)
    assert resolve_company_name(f, "hevodata.com", None, lambda: False) == ("hevodata", "domain_stem")
    assert f.urls == []


def test_news_queries_use_resolved_name(icp):
    f = ClearbitFetcher(CLEARBIT)
    res = AgentLoop(icp, "hevodata.com", f, FakeLLM(), today=TODAY).run()
    assert res.company == {"name": "Hevo Data", "source": "clearbit"}
    news_urls = [u for u in f.urls if "bing.com/news" in u]
    assert news_urls and all("Hevo+Data" in u for u in news_urls)
