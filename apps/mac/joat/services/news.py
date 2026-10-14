"""The News tab: free feeds, fetched and read on the Mac.

The wearer's ruling: why pay for a news API when the news is free. So this
is RSS — the publishers' own key-free feeds in news_sources.json, grouped by
category with a few on by default (read end to end on 2026-10-14) — fetched
here, never on the glasses (the Even WebView enforces CORS and the
publishers don't send the header). The Mac polls on a timer, keeps the
newest headlines in memory, and pushes them to the plugin, which caches
them so the tab paints from memory.

Which categories and sources, and any feeds of the wearer's own, come from
a small JSON file the JOAT Mac app writes (its News page lists the same
catalog); the poller re-reads it when it changes, so a change shows up on
the next refresh without a restart.

Opening a headline fetches that one article, extracts its text
(trafilatura) and tidies it into one template for every publisher
(`tidy_article`). Nothing is fetched speculatively.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
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
#: An article PAGE is markup, scripts and inline data around the story:
#: Live Science's ran 2.06 MB (2026-10-14), so under the feed cap every
#: one of its stories fell back to the feed's two-line summary.
MAX_PAGE_BYTES = 8_000_000
FETCH_TIMEOUT_S = 12.0
#: Default poll cadence; the config file can shorten or lengthen it.
DEFAULT_REFRESH_MINUTES = 15
MIN_REFRESH_MINUTES = 5
#: The rail's list holds twenty rows (HudList cap). Keep the newest twenty.
MAX_HEADLINES = 20
#: HudList caps a row at 64 characters; the title gets what the source
#: and age leave.
ROW_CHARACTERS = 64

CATALOG_PATH = Path(__file__).with_name("news_sources.json")


@dataclass(frozen=True, slots=True)
class NewsSource:
    """One publisher's feed in one category (news_sources.json)."""

    id: str
    category: str
    name: str
    url: str
    default: bool


def _load_catalog(path: Path = CATALOG_PATH) -> tuple[dict[str, tuple[NewsSource, ...]], tuple[str, ...]]:
    """news_sources.json → category id → its sources, and the default
    categories. One file for the engine and the Mac app's News page, so
    the two can never offer different sources."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    catalog: dict[str, tuple[NewsSource, ...]] = {}
    for category in raw["categories"]:
        catalog[category["id"]] = tuple(
            NewsSource(
                id=source["id"], category=category["id"], name=source["name"],
                url=source["url"], default=bool(source.get("default", False)),
            )
            for source in category["sources"]
        )
    defaults = tuple(c for c in raw.get("defaultCategories", ()) if c in catalog)
    return catalog, defaults


#: Category → its free, key-free sources; see news_sources.json for how
#: they were chosen and which are on by default.
CATALOG, DEFAULT_CATEGORIES = _load_catalog()
SOURCES_BY_ID: dict[str, NewsSource] = {
    source.id: source for sources in CATALOG.values() for source in sources
}

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
    #: The sources the wearer chose, by id. None until they change one:
    #: each enabled category then reads its default sources.
    sources: tuple[str, ...] | None = None

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
            if c in CATALOG
        )
        custom = tuple(
            url for url in _strings(raw.get("customFeeds"))
            if url.startswith(("http://", "https://"))
        )
        minutes = raw.get("refreshMinutes", DEFAULT_REFRESH_MINUTES)
        if not isinstance(minutes, int) or isinstance(minutes, bool):
            minutes = DEFAULT_REFRESH_MINUTES
        sources = (
            tuple(s for s in _strings(raw.get("sources")) if s in SOURCES_BY_ID)
            if isinstance(raw.get("sources"), list) else None
        )
        return cls(
            categories=categories or (DEFAULT_CATEGORIES if not custom else ()),
            custom_feeds=custom,
            refresh_minutes=max(MIN_REFRESH_MINUTES, min(24 * 60, minutes)),
            sources=sources,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "categories": list(self.categories),
            "customFeeds": list(self.custom_feeds),
            "refreshMinutes": self.refresh_minutes,
        }
        if self.sources is not None:
            out["sources"] = list(self.sources)
        return out

    def enabled_sources(self) -> tuple[NewsSource, ...]:
        """The catalog sources to poll: in each enabled category, exactly
        the ones the wearer chose there, or its defaults when they chose
        none there (a subject switched on later starts with its defaults;
        the Mac app never lets the last one in a subject be switched off)."""
        chosen = set(self.sources or ())
        out: list[NewsSource] = []
        for category in self.categories:
            picked = [source for source in CATALOG[category] if source.id in chosen]
            out.extend(picked or [source for source in CATALOG[category] if source.default])
        return tuple(out)

    def feeds(self) -> tuple[tuple[str, str], ...]:
        """(category, url) for every feed to poll, custom feeds last."""
        out = [(source.category, source.url) for source in self.enabled_sources()]
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


def _get(url: str, *, accept: str, limit: int = MAX_BYTES) -> bytes:
    request = urllib.request.Request(
        url, headers={"Accept": accept, "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_S) as response:
        body: bytes = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError("response body exceeds the size cap")
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
        if not title or not link or _PROMO.search(title) or _PROMO_LINK.search(link):
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
    """Synchronous: one article page → (title, readable text). On a thread.

    The text is tidied into one shape for every publisher (`tidy_article`).
    No `favor_precision`: on real pages (2026-10-14) it kept a fifth of an
    Ars Technica article and nothing at all of a PBS one, where the
    default kept the whole story; what the default lets through besides
    (captions, credits, share rows) is what `tidy_article` removes.
    """
    document = _get(url, accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.5", limit=MAX_PAGE_BYTES)
    page_text = document.decode("utf-8", errors="replace")
    text = trafilatura.extract(
        page_text,
        url=url,
        include_comments=False,
        include_tables=False,
        output_format="txt",
    )
    title = site = ""
    metadata = trafilatura.extract_metadata(page_text, default_url=url)
    if metadata is not None:
        title = str(metadata.title or "")
        site = str(metadata.sitename or "")
    title = tidy_title(title, site)
    return title, tidy_article(text or "", title=title)


#: Lines that are page furniture, not the story: share rows, "Loading...",
#: newsletter and "watch on" prompts, picture credits. Seen on the real
#: pages of the catalog's publishers (2026-10-14); matched whole-line, so a
#: sentence that merely contains the words stays.
_FURNITURE = re.compile(
    r"""^(?:
        [-–•]?\s*(?:published|updated)(?:[\s\d:].{0,40})?
      | related\s+topics
      | you\s+must\s+confirm\s+your\s+public\s+display\s+name.*
      | please\s+log\s*out\s+and\s+then\s+log\s*in\s+again.*
      | (?:log|sign)\s+in\s+to\s+comment.*
      | a\s+free\s+press\s+is\s+a\s+cornerstone.* | support\s+trusted\s+journalism.* | donate\s+now
      | loading\.{0,3}
      | recommended\s+stories | list\s+of\s+\d+\s+items?
      | (?:watch|listen|read)\s+(?:on|more|now|next)\b.*
      | popular\s+on\s+\S+.*
      | advertisement | sponsored(?:\s+content)?
      | [-–•]?\s*share(?:\s+this(?:\s+(?:article|story))?)?:? | [-–•]?\s*full\s+story
      | copy\s+(?:url|link).*
      | credit:? | image\s+(?:source|credit):?.* | photo(?:graph)?:.* | getty\s+images
      | (?:edited|written|produced|reported)\s+by:.*
      | (?:sign\s+up|subscribe)\b.*(?:newsletter|inbox|email).*
      | .{0,120}\bsubscribe\s+to\s+(?:\S+\s+){0,5}newsletter\.?
      | if\s+you\s+rely\s+on\s+our\s+team.*
      | related(?:\s+(?:articles?|stories|coverage))?:?
      | more\s+top\s+stories
      | (?-i:FOR\s?SUBSCRIBERS).*
      | npr\s+does\s+not\s+offer\s+or\s+accept\s+money.* | anyone\s+who\s+makes\s+such\s+an\s+offer.*
      | this\s+article\s+is\s+for\s+informational\s+purposes\s+only.*
      | .*\|\s*brought\s+to\s+you\s+by\b.*
      | .*</?script\b.*
      | sign\s+up | sign\s+up\s+today\b.* | .{0,40}\bnewsletter
      | \((?:soundbite|music|archived\s+recording|laughter|applause)\b[^)]*\) | [\w .'-]{2,40}:\s*\(singing\).*
      | every\s+day\s+we\s+strive\s+to\s+produce\s+journalism.* | .*\bpossible\s+without\s+your\s+support\.?
    )$""",
    re.IGNORECASE | re.VERBOSE,
)
#: A label whose value is on the next line ("- Date:" / "- Oct 14"). Not
#: "Share:": on ScienceDaily the story's first paragraph follows it.
_LABELLED_VALUE = re.compile(r"^[-–•]?\s*(?:date|source):?$", re.IGNORECASE)
#: A label that introduces a dek worth keeping ("- Summary:").
_LABEL_ONLY = re.compile(r"^[-–•]?\s*summary:?$", re.IGNORECASE)
#: An editor's credit line under a name ("Sadie Harley" / "Scientific Editor").
_ROLE = re.compile(
    r"^(?:scientific|chief|senior|managing|associate|contributing|deputy|science)?\s*editor$",
    re.IGNORECASE,
)
#: A dateline standing alone near the top ("October 14, 2026").
_BARE_DATE = re.compile(r"^(?:[A-Z][a-z]+\.? \d{1,2}, \d{4}|\d{1,2} [A-Z][a-z]+ \d{4})$")
#: The closing author bio ("Jon is a Senior IT Reporter for Ars Technica…").
_AUTHOR_BIO = re.compile(
    r"^[A-Z][^.]{0,60}\b(?:is (?:an? |the )?(?:[\w-]+ ){0,4}"
    r"(?:[Rr]eporter|[Ww]riter|[Ee]ditor|[Cc]orrespondent|[Jj]ournalist|[Cc]olumnist|[Oo]wner)\b"
    r"|earned (?:her|his|their|a) (?:PhD|Ph\.D\.|doctorate|master's|degree)\b)",
)
#: Production credits that close a story ("With additional reporting by…",
#: "X produced and edited this interview for broadcast."). Only the last
#: few paragraphs are tested, so a sentence in the story that reads alike
#: stays.
_CREDIT = re.compile(
    r"^(?:(?:with\s+)?additional\s+reporting\s+(?:by|from)\b.*"
    r"|design\s+and\s+development\s+by\b.*"
    r"|information\s+from\b.*\bwas\s+used\s+in\s+this\s+report.*"
    r"|this\s+(?:newsletter|story|article|episode)\s+was\s+(?:edited|produced)\s+by\b.*"
    r"|.*\bproduced\s+(?:and\s+edited\s+)?(?:this|the)\s+(?:interview|segment|video|audio|story)\b.*"
    r"|.*\badapted\s+it\s+for\s+the\s+web\.?)$",
    re.IGNORECASE,
)
#: Where a story's end matter starts: citations, reference blocks, the
#: transcript notice, reader call-outs and sign-in widgets.
_END_MATTER = re.compile(
    r"^(?:story\s+source:?|cite\s+this\s+page:?|journal\s+reference:?|"
    r"publication\s+details|more\s+information:?|provided\s+by\b.*|"
    r"copyright\s*(?:©|\(c\)).*|get\s+in\s+touch|copy\s+to\s+clipboard|sign\s+in\s+or\s+register|"
    r".*\(\d{4}[a-z]?\)\.\s.*\bdoi\.org/\S+)$",
    re.IGNORECASE,
)
#: Where the story certainly ended, however early: a "republish this
#: story" box (its HTML as text, then "Copy to Clipboard") or a sign-in
#: widget (Oklahoma Watch, 2026-10-14).
_HARD_END = re.compile(
    r"^(?:copy\s+to\s+clipboard|sign\s+in\s+or\s+register|republish\s+this\s+(?:story|article))$"
    r"|</?(?:p|a|div|img|span|script|iframe|figure|h[1-6])\b[^>]*>",
    re.IGNORECASE,
)
#: Feed items that are advertising, not stories: sportsbook promo codes in
#: a sports feed, shopping guides in a top-stories feed (2026-10-14).
_PROMO = re.compile(r"\bpromo\s+code\b|\bbonus\s+bets?\b", re.IGNORECASE)
_PROMO_LINK = re.compile(r"/(?:select/)?shopping/|/deals/", re.IGNORECASE)


def tidy_title(title: str, site: str = "") -> str:
    """The headline without the publisher tacked on ("… - Engadget")."""
    title = " ".join(title.split())
    match = re.match(r"^(.*\S)\s+[-–|]\s+([^-–|]{2,40})$", title)
    if match and site:
        tail, name = match.group(2).casefold(), site.casefold()
        if tail in name or name in tail:
            return match.group(1)
    return title


def _key(line: str) -> str:
    return re.sub(r"[\W_]+", "", line.casefold())


def _words(line: str) -> set[str]:
    return set(re.findall(r"\w+", line.casefold()))


def _restates(line: str, title: str) -> bool:
    """A second wording of the headline (the feed's, above the page's own):
    short, no closing full stop, and most of its words are the headline's."""
    if not title or len(line) > 200 or line.rstrip().endswith("."):
        return False
    mine, theirs = _words(line), _words(title)
    shorter = min(len(mine), len(theirs))
    return shorter >= 4 and len(mine & theirs) >= 0.6 * shorter


def tidy_article(text: str, *, title: str = "") -> str:
    """Any publisher's extracted text in one shape: the story's own
    paragraphs, each once, separated by a blank line, without the page's
    furniture, picture captions, repeated headline, or end matter.

    Picture captions are recognised by the credit line that follows them
    on the page; a line repeated within a few lines (the same caption for
    two screen sizes) is kept once.
    """
    lines = [" ".join(line.split()) for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    lines = [line for line in lines if line]
    end = next((i for i, line in enumerate(lines) if i >= len(lines) * 0.6 and _END_MATTER.match(line)), len(lines))
    end = next((i for i, line in enumerate(lines[:end]) if i >= 2 and _HARD_END.search(line)), end)
    lines = lines[:end]
    kept: list[str] = []
    recent: list[str] = []
    title_key = _key(title)
    skip_next = False
    dek_next = False
    for index, line in enumerate(lines):
        if skip_next:
            skip_next = False
            continue
        following = lines[index + 1] if index + 1 < len(lines) else ""
        if _LABELLED_VALUE.match(line):
            skip_next = True
            continue
        if _LABEL_ONLY.match(line):
            dek_next = True
            continue
        if dek_next:
            # The dek is a paragraph, not a list item ("- Two cats…").
            line = re.sub(r"^[-–•]\s+", "", line)
            dek_next = False
        if title and len(line) > len(title) + 20 and line.startswith(title):
            # A teaser card: the headline run into its summary
            # ("…press secretaryKatie Zacharia's first…"); the summary
            # alone is kept, and once.
            line = line[len(title):].lstrip(" :–-")
        if re.fullmatch(r"credit:?", following, re.IGNORECASE):
            # A caption, then "Credit:" and the credit itself.
            continue
        if re.fullmatch(r"credit:?", line, re.IGNORECASE):
            skip_next = len(following) <= 60
            continue
        if _FURNITURE.match(line) or sum(ch.isalnum() for ch in line) < 2:
            continue
        if re.fullmatch(r"[A-Z]{2,6}(?:/[A-Z]{2,6})+", line):
            continue  # a wire credit under a live-blog entry ("CBS/AP")
        if _ROLE.match(line):
            # The name above it was the credit's, not the story's.
            if kept and len(kept[-1].split()) <= 4 and not kept[-1].endswith("."):
                kept.pop()
            continue
        if len(kept) < 3 and (_BARE_DATE.match(line) or _restates(line, title)):
            continue
        key = _key(line)
        if not key or key == title_key or key in recent:
            continue
        recent = [*recent[-7:], key]
        kept.append(re.sub(r"^[–•]\s+", "- ", line))
    # The close: production credits, then the author's bio (one or two
    # paragraphs) — never the story's only paragraph.
    while len(kept) > 1 and _CREDIT.match(kept[-1]):
        kept.pop()
    for start in range(max(1, len(kept) - 2), len(kept)):
        if _AUTHOR_BIO.match(kept[start]):
            del kept[start:]
            break
    return "\n\n".join(kept)


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
        config = self.config
        feeds = config.feeds()
        # A catalog feed's rows carry the publisher's catalog name, not the
        # feed's own title ("World", "All Top News --…", "Latest from Live…").
        names = {source.url: source.name for source in config.enabled_sources()}
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
                # Name the feed's host (never the path or query) and the HTTP
                # status: "category=custom (HTTPError)" logged ~1,900 times
                # without saying which of four custom feeds was dead.
                status = getattr(result, "code", None)
                log.info(
                    "feed unavailable category=%s host=%s (%s%s)",
                    category,
                    urllib.parse.urlsplit(url).hostname or "?",
                    type(result).__name__,
                    f" {status}" if isinstance(status, int) else "",
                )
                continue
            name = names.get(url)
            merged.extend(replace(h, source=name) if name else h for h in result)
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
        lead, _, rest = text.partition("\n\n")
        if rest and (_key(lead) == _key(headline.title) or _restates(lead, headline.title)):
            # The feed's wording of the headline, above the page's own
            # (BBC, 2026-10-14): the template shows one headline.
            text = rest
        if not text and headline.summary and headline.summary.casefold() != headline.title.casefold():
            text = f"{headline.summary}\n\n(From the feed's summary; the full article could not be read on the Mac.)"
        body = text or "The article text could not be read on the Mac."
        # The article template (PanelPublisher): headline, then source and
        # age, then the tidied paragraphs.
        return Reader(
            tab=self.tab,
            title="NEWS · ARTICLE",
            metadata=f"{headline.source} · {relative_age(headline.published_at, self._clock())}",
            headline=title or headline.title,
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
