"""The News tab: free feeds, fetched and read on the Mac.

The wearer's ruling: why pay for a news API when the news is free. So this
is RSS — BBC, NPR and Google News topic feeds, every one of them key-free
and verified live on 2026-09-02 — fetched here, never on the glasses (the
Even WebView enforces CORS and none of those publishers send the header).
The Mac polls on a timer, keeps the newest headlines in memory, and pushes
them to the plugin, which caches them so the tab paints from memory.

Which categories, and any feeds of the wearer's own, come from a small JSON
file the JOAT Mac app writes; the poller re-reads it when it changes, so a
new category shows up on the next refresh without a restart.

Opening a headline fetches that one article and extracts its text
(trafilatura) for the reader. Nothing is fetched speculatively.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import html
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

import feedparser
import trafilatura

from .panels import PanelSnapshot, Reader, paginate

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / "Library/Application Support/JOAT/news.json"
USER_AGENT = "JOAT-G2/0.1 (+personal reader)"
#: A feed larger than this is not a feed. Same cap the verifier uses.
MAX_BYTES = 2_000_000
FETCH_TIMEOUT_S = 12.0
#: Default poll cadence; the config file can shorten or lengthen it.
DEFAULT_REFRESH_MINUTES = 15
MIN_REFRESH_MINUTES = 5
#: The rail's list holds twenty rows (HudList cap). Keep the newest twenty.
MAX_HEADLINES = 20
#: HudList caps a row at 64 characters; the title gets what the source
#: and age leave.
ROW_CHARACTERS = 64

_GOOGLE = "https://news.google.com/rss/headlines/section/topic/{topic}?hl=en-US&gl=US&ceid=US:en"
#: Category → key-free feeds. Three sources per category so one outage
#: never empties the tab. Order is the order they were verified.
CATEGORY_FEEDS: dict[str, tuple[str, ...]] = {
    "top": (
        "https://feeds.bbci.co.uk/news/rss.xml",
        "https://feeds.npr.org/1001/rss.xml",
        "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
    ),
    "world": (
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://feeds.npr.org/1004/rss.xml",
        _GOOGLE.format(topic="WORLD"),
    ),
    "us": (
        "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml",
        "https://feeds.npr.org/1003/rss.xml",
        _GOOGLE.format(topic="NATION"),
    ),
    "business": (
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://feeds.npr.org/1006/rss.xml",
        _GOOGLE.format(topic="BUSINESS"),
    ),
    "technology": (
        "https://feeds.bbci.co.uk/news/technology/rss.xml",
        "https://feeds.npr.org/1019/rss.xml",
        _GOOGLE.format(topic="TECHNOLOGY"),
    ),
    "science": (
        "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
        "https://feeds.npr.org/1007/rss.xml",
        _GOOGLE.format(topic="SCIENCE"),
    ),
    "health": (
        "https://feeds.bbci.co.uk/news/health/rss.xml",
        "https://feeds.npr.org/1128/rss.xml",
        _GOOGLE.format(topic="HEALTH"),
    ),
    "sports": (
        "https://feeds.bbci.co.uk/sport/rss.xml",
        "https://feeds.npr.org/1055/rss.xml",
        _GOOGLE.format(topic="SPORTS"),
    ),
    "entertainment": (
        "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml",
        "https://feeds.npr.org/1008/rss.xml",
        _GOOGLE.format(topic="ENTERTAINMENT"),
    ),
}
DEFAULT_CATEGORIES: tuple[str, ...] = ("top", "technology", "world")

#: Feed titles as the publishers write them → the short name the row shows.
_SOURCE_NAMES: tuple[tuple[str, str], ...] = (
    ("bbc", "BBC"),
    ("npr", "NPR"),
    ("google news", "Google News"),
    ("guardian", "Guardian"),
    ("new york times", "NYT"),
    ("nyt", "NYT"),
    ("al jazeera", "Al Jazeera"),
    ("reuters", "Reuters"),
    ("associated press", "AP"),
    ("ars technica", "Ars Technica"),
    ("hacker news", "Hacker News"),
    ("espn", "ESPN"),
)


@dataclass(frozen=True, slots=True)
class NewsConfig:
    categories: tuple[str, ...] = DEFAULT_CATEGORIES
    custom_feeds: tuple[str, ...] = ()
    refresh_minutes: int = DEFAULT_REFRESH_MINUTES

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> NewsConfig:
        """Read the Mac app's file. Missing or malformed → the defaults,
        because an unreadable preference must not blank the tab."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as exc:
            log.warning("news config unreadable (%s); using defaults", type(exc).__name__)
            return cls()
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: Any) -> NewsConfig:
        if not isinstance(raw, dict):
            return cls()
        categories = tuple(
            c for c in _strings(raw.get("categories"))
            if c in CATEGORY_FEEDS
        )
        custom = tuple(
            url for url in _strings(raw.get("customFeeds"))
            if url.startswith(("http://", "https://"))
        )
        minutes = raw.get("refreshMinutes", DEFAULT_REFRESH_MINUTES)
        if not isinstance(minutes, int) or isinstance(minutes, bool):
            minutes = DEFAULT_REFRESH_MINUTES
        return cls(
            categories=categories or (DEFAULT_CATEGORIES if not custom else ()),
            custom_feeds=custom,
            refresh_minutes=max(MIN_REFRESH_MINUTES, min(24 * 60, minutes)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "categories": list(self.categories),
            "customFeeds": list(self.custom_feeds),
            "refreshMinutes": self.refresh_minutes,
        }

    def feeds(self) -> tuple[tuple[str, str], ...]:
        """(category, url) for every feed to poll, custom feeds last."""
        out: list[tuple[str, str]] = []
        for category in self.categories:
            out.extend((category, url) for url in CATEGORY_FEEDS[category])
        out.extend(("custom", url) for url in self.custom_feeds)
        return tuple(out)


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


@dataclass(frozen=True, slots=True)
class Headline:
    title: str
    link: str
    source: str
    category: str
    published_at: float
    guid: str
    #: The feed's own description, tags stripped; empty when it had none.
    summary: str = ""

    def row(self, now: float | None = None) -> str:
        """"[3m] Headline · BBC" — the dense-row grammar the glass parses."""
        age = relative_age(self.published_at, now)
        suffix = f" · {self.source}" if self.source else ""
        budget = ROW_CHARACTERS - len(f"[{age}] ") - len(suffix)
        return f"[{age}] {shorten(self.title, max(12, budget))}{suffix}"


def relative_age(at: float, now: float | None = None) -> str:
    elapsed = max(0.0, (now if now is not None else time.time()) - at)
    if elapsed < 60:
        return "now"
    if elapsed < 3_600:
        return f"{int(elapsed // 60)}m"
    if elapsed < 86_400:
        return f"{int(elapsed // 3_600)}h"
    return f"{int(elapsed // 86_400)}d"


def shorten(text: str, limit: int) -> str:
    """At most `limit` characters, ellipsis included, cut on a word."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    head = text[: max(1, limit - 1)]
    cut = head.rsplit(" ", 1)[0] if " " in head else head
    return (cut or head).rstrip(",.;: ") + "…"


def _source_name(feed_title: str, entry_source: str) -> str:
    """Google News names the publisher per entry; BBC and NPR per feed."""
    candidate = entry_source or feed_title
    lowered = candidate.lower()
    for key, name in _SOURCE_NAMES:
        if key in lowered:
            return name
    # "BBC News - Technology" → "BBC News"; keep the first clause short.
    head = re.split(r"\s+[-–|:]\s+", candidate, maxsplit=1)[0].strip()
    return shorten(head, 18)


def _get(url: str, *, accept: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"Accept": accept, "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_S) as response:
        body: bytes = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("feed body exceeds the size cap")
    return body


def fetch_feed(url: str, category: str, *, now: float | None = None) -> list[Headline]:
    """Synchronous: one feed → its headlines, newest first. Call on a thread."""
    body = _get(url, accept="application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5")
    parsed = feedparser.parse(body)
    feed_title = str(parsed.feed.get("title", "") or "")
    reference = now if now is not None else time.time()
    headlines: list[Headline] = []
    for entry in parsed.entries:
        title = " ".join(str(entry.get("title", "") or "").split())
        link = str(entry.get("link", "") or "")
        if not title or not link:
            continue
        struct = entry.get("published_parsed") or entry.get("updated_parsed")
        published = _struct_to_unix(struct, reference)
        source_field = entry.get("source")
        entry_source = ""
        if isinstance(source_field, dict):
            entry_source = str(source_field.get("title", "") or "")
            # Google News titles carry the publisher; the row names it.
            title = _strip_publisher(title)
        # The feed's own description is the fallback article (the audit's
        # N2): when the publisher page cannot be extracted, the wearer
        # gets the feed's summary rather than the headline repeated.
        summary_html = str(entry.get("summary", "") or entry.get("description", "") or "")
        summary = _summary_text(summary_html)
        headlines.append(Headline(
            title=title,
            link=link,
            source=_source_name(feed_title, entry_source),
            category=category,
            published_at=published,
            guid=str(entry.get("id", "") or link),
            summary=summary[:2_000],
        ))
    headlines.sort(key=lambda h: h.published_at, reverse=True)
    return headlines


def _summary_text(summary_html: str) -> str:
    """Feed HTML → readable text for the glass.

    The old tag-strip left entities in place, so a Google News summary
    (an <ol> of related coverage separated by ``&nbsp;&nbsp;``) reached the
    wearer as one run-on line with literal "&nbsp;" in it (observed on the
    simulator, 2026-09-22). Block ends become line breaks so each related
    headline is its own row, and entities are decoded.
    """
    broken = re.sub(r"(?i)</(li|p|div|h[1-6]|tr)\s*>|<br\s*/?>", "\n", summary_html)
    stripped = re.sub(r"<[^>]+>", " ", broken)
    lines = (" ".join(html.unescape(line).split()) for line in stripped.split("\n"))
    return "\n".join(line for line in lines if line)


def _struct_to_unix(struct: Any, fallback: float) -> float:
    if struct is None:
        return fallback
    try:
        import calendar

        return float(calendar.timegm(struct))
    except (TypeError, ValueError, OverflowError):
        return fallback


def fetch_article_text(url: str) -> tuple[str, str]:
    """Synchronous: one article page → (title, readable text). On a thread."""
    page = _get(url, accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.5")
    text = trafilatura.extract(
        page.decode("utf-8", errors="replace"),
        url=url,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
        output_format="txt",
    )
    title = ""
    metadata = trafilatura.extract_metadata(page.decode("utf-8", errors="replace"), default_url=url)
    if metadata is not None and metadata.title:
        title = str(metadata.title)
    return title, (text or "").strip()


FetchFeed = Callable[[str, str], list[Headline]]
FetchArticle = Callable[[str], tuple[str, str]]


class NewsService:
    """Owns the headlines: polls the feeds, serves the tab, opens a story."""

    tab = "news"

    def __init__(
        self,
        config_path: Path = DEFAULT_CONFIG_PATH,
        *,
        fetch_feed: FetchFeed = fetch_feed,
        fetch_article: FetchArticle = fetch_article_text,
        clock: Callable[[], float] = time.time,
        max_headlines: int = MAX_HEADLINES,
    ) -> None:
        self._config_path = config_path
        self._fetch_feed = fetch_feed
        self._fetch_article = fetch_article
        self._clock = clock
        self._max = max_headlines
        self._config = NewsConfig.load(config_path)
        self._config_mtime = self._mtime()
        self._headlines: list[Headline] = []
        self._fetched_at: float | None = None
        self._last_error: str | None = None
        self._partial = False
        self._running = False
        self._refresh_lock = asyncio.Lock()

    # ── Configuration ────────────────────────────────────────────────────

    @property
    def config(self) -> NewsConfig:
        """The Mac app's current choice, re-read when the file changes."""
        mtime = self._mtime()
        if mtime != self._config_mtime:
            self._config = NewsConfig.load(self._config_path)
            self._config_mtime = mtime
            log.info("news config reloaded categories=%s", ",".join(self._config.categories))
        return self._config

    def _mtime(self) -> float | None:
        try:
            return os.stat(self._config_path).st_mtime
        except OSError:
            return None

    # ── The headlines ────────────────────────────────────────────────────

    @property
    def headlines(self) -> tuple[Headline, ...]:
        return tuple(self._headlines)

    @property
    def fetched_at(self) -> float | None:
        return self._fetched_at

    @property
    def last_error(self) -> str | None:
        """Why the newest pass could not refresh, or None when it did."""
        return self._last_error

    @property
    def partial(self) -> bool:
        """True when the newest pass lost some feeds but kept others."""
        return self._partial

    async def refresh_once(self) -> int:
        """Fetch every configured feed, each on its own thread, each failing
        alone. Returns how many headlines the tab now holds."""
        await self.refresh_changed()
        return len(self._headlines)

    async def refresh_changed(self) -> bool:
        async with self._refresh_lock:
            return await self._refresh_changed()

    async def _refresh_changed(self) -> bool:
        """One pass; True when what the wearer would see changed.

        The audit's N1: a pass that kept every feed's old headlines still
        published, and the plugin stamps "updated now" on every push — so a
        failed refresh looked fresh. Now a pass reports whether the
        headlines OR the failure state changed, and only that publishes.
        """
        feeds = self.config.feeds()
        fetching = asyncio.gather(
            *(asyncio.to_thread(self._fetch_feed, url, category) for category, url in feeds),
            return_exceptions=True,
        )
        try:
            results = await asyncio.shield(fetching)
        except asyncio.CancelledError:
            # urllib worker threads cannot be cancelled. Retain the refresh
            # lock until they drain, even after repeated cancellation, and
            # discard their result so supersession cannot commit stale news.
            while not fetching.done():
                try:
                    await asyncio.shield(fetching)
                except asyncio.CancelledError:
                    continue
            raise
        merged: list[Headline] = []
        failures = 0
        for (category, url), result in zip(feeds, results, strict=True):
            if isinstance(result, BaseException):
                failures += 1
                log.info("feed unavailable category=%s (%s)", category, type(result).__name__)
                continue
            merged.extend(result)
        before = (tuple(h.guid for h in self._headlines), self._last_error, self._partial)
        if failures and not merged and self._headlines:
            # Every feed failed: keep what the wearer already has, and say
            # so. fetched_at stays where it was — nothing was fetched.
            self._last_error = "feeds unavailable"
            self._partial = False
        else:
            self._headlines = _dedupe(merged, self._clock())[: self._max]
            self._fetched_at = self._clock()
            self._last_error = "feeds unavailable" if failures == len(feeds) and feeds else None
            self._partial = 0 < failures < len(feeds)
        after = (tuple(h.guid for h in self._headlines), self._last_error, self._partial)
        return before != after

    async def refresh(self) -> None:
        await self.refresh_once()

    async def run(self, on_refreshed: Callable[[], Any] | None = None) -> None:
        """Poll on the configured cadence, publishing only when a pass changed
        what the wearer would see (the plugin dates the panel by its push)."""
        self._running = True
        first_pass = True
        while self._running:
            changed = False
            try:
                changed = await self.refresh_changed()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("news refresh failed", exc_info=True)
            if on_refreshed is not None and (changed or first_pass):
                try:
                    result = on_refreshed()
                    if asyncio.iscoroutine(result):
                        await result
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.warning("news publish failed", exc_info=True)
            first_pass = False
            await asyncio.sleep(self.config.refresh_minutes * 60)

    def stop(self) -> None:
        self._running = False

    # ── The tab ──────────────────────────────────────────────────────────

    def snapshot(self) -> PanelSnapshot:
        now = self._clock()
        if not self._headlines:
            empty = (
                "Feeds unavailable — hold to retry."
                if self._last_error
                else "No headlines yet — long-press to refresh."
            )
            return PanelSnapshot(rows=(), empty=empty)
        rows = tuple(h.row(now) for h in self._headlines)
        # A kept-but-stale list says so on its own first row: the plugin's
        # "updated now" is when it was pushed, not when these were fetched.
        if self._last_error and self._fetched_at is not None:
            age = relative_age(self._fetched_at, now)
            return PanelSnapshot(rows=(f"[!] Feeds unavailable · showing headlines from {age} ago", *rows))
        if self._partial:
            return PanelSnapshot(rows=("[!] Some feeds unavailable · partial list", *rows))
        return PanelSnapshot(rows=rows)

    @property
    def status_rows(self) -> int:
        """Rows the tab shows above its headlines: the "[!]" feed status.
        A tap's row index counts them; a headline index does not."""
        if not self._headlines:
            return 0
        return 1 if (self._last_error and self._fetched_at is not None) or self._partial else 0

    async def open(self, index: int) -> Reader | None:
        """Open headline `index` (not a tab row: see `status_rows`)."""
        if not 0 <= index < len(self._headlines):
            return None
        headline = self._headlines[index]
        try:
            title, text = await asyncio.to_thread(self._fetch_article, headline.link)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            log.info("article unavailable (%s)", type(exc).__name__)
            title, text = headline.title, ""
        if not text and headline.summary and headline.summary.casefold() != headline.title.casefold():
            text = f"{headline.summary}\n\n(From the feed's summary; the full article could not be read on the Mac.)"
        body = text or (
            "The article text could not be read on the Mac. "
            f"Headline: {headline.title}"
        )
        metadata = f"{headline.source} · {relative_age(headline.published_at, self._clock())}"
        # No fixed pre-cut: the metadata row is clipped to one reader row
        # downstream (panels), so a 40-char cap only threw away title the
        # row could still show (2026-09-05 typography audit).
        shown_title = title or headline.title
        return Reader(
            tab=self.tab,
            title="NEWS · ARTICLE",
            metadata=f"{metadata} · {shown_title}",
            pages=paginate(body),
        )

    # ── Voice ────────────────────────────────────────────────────────────

    def spoken_headlines(self, limit: int = 5) -> str:
        """What JOAT says for "what's in the news": short, numbered, from the
        cache — it must never wait on a fetch to answer."""
        if not self._headlines:
            return "I don't have any headlines yet."
        parts = []
        for number, headline in enumerate(self._headlines[:limit], start=1):
            source = f" from {headline.source}" if headline.source else ""
            parts.append(f"{number}. {headline.title}{source}.")
        return "Here are the headlines. " + " ".join(parts)

    def headline_at(self, index: int) -> Headline | None:
        return self._headlines[index] if 0 <= index < len(self._headlines) else None


def _dedupe(headlines: list[Headline], now: float) -> list[Headline]:
    """Newest first, one row per story across feeds. Google News repeats
    what BBC just said; the wearer should read it once."""
    seen: set[str] = set()
    out: list[Headline] = []
    for headline in sorted(headlines, key=lambda h: h.published_at, reverse=True):
        key = _story_key(headline)
        if key in seen:
            continue
        seen.add(key)
        # A feed that lies about the future is still shown, just not ahead
        # of what actually happened.
        if headline.published_at > now + 3_600:
            headline = Headline(
                title=headline.title, link=headline.link, source=headline.source,
                category=headline.category, published_at=now, guid=headline.guid,
            )
        out.append(headline)
    return out


def _story_key(headline: Headline) -> str:
    words = re.findall(r"[a-z0-9]+", _strip_publisher(headline.title).lower())
    return " ".join(words[:8]) or headline.link


_PUBLISHER_SUFFIX = re.compile(r"\s+[-–|]\s+([^-–|]{2,40})$")


def _strip_publisher(title: str) -> str:
    """"Apple raises prices - Reuters" → "Apple raises prices". Google News
    appends the publisher to every title; the row already names it."""
    match = _PUBLISHER_SUFFIX.search(title)
    if match is None:
        return title
    head = title[: match.start()].strip()
    return head if len(head.split()) >= 3 else title


def article_domain(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host
