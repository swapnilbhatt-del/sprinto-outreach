"""HTML -> plain text: drop scripts, nav, footer, cookie banners and repeated boilerplate; cap length."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

DROP_TAGS = ["script", "style", "noscript", "svg", "nav", "footer", "form", "iframe", "template", "aside"]
BOILERPLATE_ATTR = re.compile(r"cookie|consent|banner|navbar|menu|footer|breadcrumb|newsletter|modal|popup", re.I)


def clean_html(html: str, max_chars: int) -> str:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for tag in soup(DROP_TAGS):
        tag.decompose()
    for el in soup.find_all(attrs={"class": BOILERPLATE_ATTR}) + soup.find_all(attrs={"id": BOILERPLATE_ATTR}):
        if el.name not in ("body", "main", "html"):
            el.decompose()
    lines, seen = [], set()
    for line in soup.get_text("\n").splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if len(line) < 3 or line.lower() in seen:
            continue
        seen.add(line.lower())
        lines.append(line)
    text = "\n".join(([title] if title else []) + lines)
    return text[:max_chars]


def extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """(absolute_url, anchor_text) for every link. Run on raw HTML, before clean drops nav/footer."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip())
        if urlparse(href).scheme in ("http", "https"):
            out.append((href.split("#")[0], a.get_text(" ", strip=True)[:80]))
    return out


ATTESTATION = re.compile(r"soc\s?-?\s?2|iso\s?/?\s?(iec\s?)?27001|pci[\s-]?dss|hipaa|certif|trust\s?center|security", re.I)


def attestation_links(html: str, base_url: str, domain: str, limit: int = 12) -> list[str]:
    """Link texts that mention SOC 2, ISO 27001, PCI, certificates or security, as "text (url)" lines,
    so a claim made only in a footer link survives cleaning. Same-site and trust-center links only."""
    out, seen = [], set()
    for href, text in extract_links(html, base_url):
        t = " ".join((text or "").split())
        host = urlparse(href).netloc.lower().removeprefix("www.")
        if not t or len(t) > 120 or not (host == domain or host.endswith("." + domain)):
            continue
        if ATTESTATION.search(t) or ATTESTATION.search(urlparse(href).path):
            line = f"{t} ({href})"
            if line not in seen:
                seen.add(line)
                out.append(line)
        if len(out) >= limit:
            break
    return out
