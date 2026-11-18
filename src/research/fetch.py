"""HTTP fetch: timeout, identifying user agent, robots.txt, and no CAPTCHA/login bypass."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

CHALLENGE_MARKERS = re.compile(
    r"cf-challenge|challenge-platform|g-recaptcha|h-captcha|hcaptcha\.com|"
    r"<title>\s*just a moment|attention required|verify you are human|please sign in to continue",
    re.I,
)


@dataclass
class FetchResult:
    url: str
    outcome: str              # ok | not_found | robots_blocked | challenge | error | off_domain
    status: int | None = None
    text: str = ""
    final_url: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


class Fetcher:
    def __init__(self, user_agent: str, timeout: float = 15.0, transport: httpx.BaseTransport | None = None):
        self.user_agent = user_agent
        self.client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Language": "en"},
            timeout=timeout, follow_redirects=True, transport=transport,
        )
        self._robots: dict[str, RobotFileParser] = {}

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        rp = self._robots.get(origin)
        if rp is None:
            rp = RobotFileParser()
            try:
                r = self.client.get(origin + "/robots.txt")
                if r.status_code in (401, 403):
                    rp.disallow_all = True
                elif r.status_code >= 400:
                    rp.allow_all = True
                else:
                    rp.parse(r.text.splitlines())
            except httpx.HTTPError:
                rp.allow_all = True        # unreachable robots.txt: treat as no restrictions (RFC 9309)
            self._robots[origin] = rp
        return rp.can_fetch(self.user_agent, url)

    def get(self, url: str, allowed_hosts: tuple[str, ...] = ()) -> FetchResult:
        """Fetch one URL. `allowed_hosts`: the final URL after redirects must end with one of these."""
        if not self.allowed(url):
            return FetchResult(url, "robots_blocked")
        try:
            r = self.client.get(url)
        except httpx.HTTPError as e:
            return FetchResult(url, "error", error=f"{type(e).__name__}: {e}")
        final = str(r.url)
        host = urlparse(final).netloc.lower().split(":")[0]
        if allowed_hosts and not any(host == h or host.endswith("." + h) for h in allowed_hosts):
            return FetchResult(url, "off_domain", r.status_code, final_url=final)
        if r.status_code == 404 or r.status_code == 410:
            return FetchResult(url, "not_found", r.status_code, final_url=final)
        if r.status_code in (401, 403, 429) or CHALLENGE_MARKERS.search(r.text[:20000]):
            return FetchResult(url, "challenge", r.status_code, final_url=final)
        if r.status_code >= 400:
            return FetchResult(url, "error", r.status_code, final_url=final, error=f"HTTP {r.status_code}")
        return FetchResult(url, "ok", r.status_code, text=r.text, final_url=final)
