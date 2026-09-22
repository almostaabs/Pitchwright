"""Pull a few real facts off a prospect's website. Stdlib only.

Finding nothing is a normal, unremarkable outcome - we record "no data found"
and every downstream node treats that as a perfectly valid state. We never
invent a fact to fill the gap.
"""
from __future__ import annotations

import re
import urllib.error
import urllib.request
from html.parser import HTMLParser

NO_DATA = "no data found"
_SKIP_TAGS = {"script", "style", "noscript", "svg", "head"}
_UA = "Mozilla/5.0 (compatible; sponsorship-outreach-agent/1.0)"
MAX_CHARS = 1500


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.chunks: list[str] = []
        self.meta: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        if tag == "meta":
            a = dict(attrs)
            if a.get("name") in {"description", "keywords"} or a.get("property") == "og:description":
                if a.get("content"):
                    self.meta.append(a["content"].strip())

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._skip:
            return
        text = data.strip()
        if len(text) > 2:
            self.chunks.append(text)


def _normalize_url(website: str) -> str:
    website = website.strip()
    if not re.match(r"^https?://", website, re.I):
        website = "https://" + website
    return website


def fetch_site_text(website: str, timeout: int = 10) -> str:
    """Return trimmed visible text, or "" if the site is unreachable/empty."""
    if not website or not str(website).strip():
        return ""
    request = urllib.request.Request(_normalize_url(str(website)), headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if "html" not in response.headers.get("Content-Type", "text/html"):
                return ""
            raw = response.read(400_000).decode(
                response.headers.get_content_charset() or "utf-8", errors="replace"
            )
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return ""

    parser = _TextExtractor()
    try:
        parser.feed(raw)
    except Exception:
        return ""
    text = " ".join(parser.meta + parser.chunks)
    return re.sub(r"\s+", " ", text).strip()[:MAX_CHARS]


def research(prospect: dict) -> str:
    """Website text if reachable, else the notes column, else NO_DATA."""
    text = fetch_site_text(prospect.get("website", ""))
    if text:
        return f"From {prospect.get('website')}: {text}"
    notes = str(prospect.get("notes", "") or "").strip()
    if notes:
        return f"From operator notes (website unreachable or empty): {notes}"
    return NO_DATA
