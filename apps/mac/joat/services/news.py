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

A headline is listed only after its article was fetched and read in full
here (trafilatura, tidied into one template for every publisher by
`tidy_article`; at least `MIN_ARTICLE_WORDS` of story, no paywall wording):
a feed that is down, blocks us, or whose stories are walled or stubs adds
nothing to the list instead of a row that opens to a summary or an
apology. Such feeds are benched for a while rather than polled forever. The
list is shared out evenly across the wearer's subjects, newest first, and
opening a row costs nothing: its article was read when the list was built.
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

#: A story is listed only once its page read as at least this many words
#: (counting characters/6 for scripts written without spaces). The shortest
#: real stories on the catalog's publishers ran ~150 on 2026-10-15; a video
#: hub, a paywall stub or a login wall runs far under.
MIN_ARTICLE_WORDS = 120
#: Story pages read at once in all, and at once from one publisher
#: (phys.org answers a burst from one address with 429).
VERIFY_WORKERS = 8
VERIFY_PER_HOST = 2
#: After this long a pass starts no more reads. The first pass (and a pass
#: the wearer asked for) is shorter, so the tab fills quickly; the pass
#: after it finds those reads cached and finishes the list.
VERIFY_DEADLINE_S = 45.0
FIRST_PASS_DEADLINE_S = 20.0
CATCH_UP_DELAY_S = 60.0
MAX_CATCH_UPS = 3
#: Reads a subject may spend per pass finding its share (a floor, and a
#: multiple of the share): enough to step over a few paywalled stories,
#: not to crawl a feed that is all of them.
MIN_ATTEMPTS = 10
ATTEMPT_FACTOR = 3
#: A page judged not readable is not asked again for this long; a read story
#: is kept while its feed still lists it.
BAD_ARTICLE_TTL_S = 30 * 60
MAX_CACHED_ARTICLES = 600
#: Three failed fetches in a row bench a feed for an hour, doubling to twelve.
FEED_FAILURES_TO_BENCH = 3
FEED_BENCH_BASE_S = 3_600.0
FEED_BENCH_MAX_S = 12 * 3_600.0
#: Six stories checked, none readable in full: benched for a day.
FEED_UNREADABLE_TO_BENCH = 6
FEED_UNREADABLE_BENCH_S = 24 * 3_600.0

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
    #: The URL of the feed it came from: health is kept per feed.
    feed: str = ""

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
        title = " ".join(_visible(str(entry.get("title", "") or "")).split())
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
        headlines.append(Headline(
            title=title,
            link=link,
            source=_source_name(feed_title, entry_source),
            category=category,
            published_at=published,
            guid=str(entry.get("id", "") or link),
        ))
    headlines.sort(key=lambda h: h.published_at, reverse=True)
    return headlines


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


#: Characters that draw nothing: zero-width spaces and joiners, direction
#: marks, the soft hyphen, the byte-order mark. Peoples Gazette seeds its
#: headlines with a run of them (2026-10-15); on the glasses they are boxes
#: that cost bytes, and a title carrying them never matches its twin.
_INVISIBLE = re.compile("[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")


def _visible(text: str) -> str:
    return _INVISIBLE.sub("", text)


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
      | discover\s+more\s+from\s+.* | latest\s+headlines | you\s+are\s+here
      | we['’]ve\s+got\s+the\s+edge\b.* | join\s+our\s+(?:whatsapp|telegram)\s+channel
      | follow\s+us\s+on\b.* | please\s+follow\s+and\s+like\s+us:? | details\s+later\.{0,3}
      | pay\s+attention:.* | photo\s+caption:.* | featured\s+image:.* | last\s+updated:.*
      | go\s+to\s+\S+\s+for\s+more\s+.* | to\s+stay\s+updated\s+on\b.*
      | (?:also\s+read|read\s+also|see\s+also):?.* | source:\s*legit\.ng
      | dive\s+brief:? | table\s+of\s+contents | (?-i:Topics\s+(?:[A-Z][\w&]*\s?){1,4})
      | [-•]?\s*.+\s›
      | ©\s*\d{4}.* | \(c\)\s+copyright.* | .*all\s+rights\s+reserved.*
      | ftc:\s+we\s+use.* | illustration:.* | timely,\s+incisive\s+articles.*
      | stay\s+up\s+to\s+date\s+with\b.*crunchbase.*
      | from\s+the\s+[\w-]+\s+dept | filed\s+under:.* | companies:\s+[\w@&,. -]{1,120}
      | breaking\s+cybersecurity\s+news,.* | dr\s+global\s+europe | save\s+your\s+seat
      | \d{1,2}\s+[a-z]+\s+\d{4}:\s*(?:published|updated).* | (?:latest|previous|next)\s+post\b.*
      | significant\s+revisions
      | \(what['’]s\s+moving\s+sensex.* | subscribe\s+to\s+et\s+prime.* | top\s+trending\s+stocks:.*
      | listen\s+to\s+this\s+article.* | follow\s+\S+\s+on\s+google\s+news.*
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
#: A byline or dateline standing alone near the top ("By Bayo Wahab",
#: "From Isaac Anumihe, Abuja", "By Emma Opara | October 14th 2026"): no
#: sentence punctuation, a handful of words.
_BYLINE = re.compile(r"^(?i:by|from)\s+[A-Z][^.!?]{1,80}$")
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
    r"|.*\badapted\s+it\s+for\s+the\s+web\.?"
    r"|source:\s*\S[^.]{0,40}"
    r"|.*\(email hidden\).*)$",
    re.IGNORECASE,
)
#: Where a story's end matter starts: citations, reference blocks, the
#: transcript notice, reader call-outs and sign-in widgets.
_END_MATTER = re.compile(
    r"^(?:story\s+source:?|cite\s+this\s+page:?|journal\s+reference:?|references?|"
    r"publication\s+details|more\s+information:?|provided\s+by\b.*|"
    r"copyright\s*(?:©|\(c\)).*|get\s+in\s+touch|copy\s+to\s+clipboard|sign\s+in\s+or\s+register|"
    r".*\(\d{4}[a-z]?\)\.\s.*\bdoi\.org/\S+)$",
    re.IGNORECASE,
)
#: Where the story certainly ended, however early: a "republish this
#: story" box (its HTML as text, then "Copy to Clipboard") or a sign-in
#: widget (Oklahoma Watch, 2026-10-14).
_HARD_END = re.compile(
    r"^(?:copy\s+to\s+clipboard|sign\s+in\s+or\s+register|republish\s+this\s+(?:story|article)"
    r"|more\s+from\s+\S.*|we\s+have\s+recently\s+deactivated\s+our\s+website.*|update\s+news:?"
    r"|press\s+information|bottom\s+line|about\s+the\s+author"
    r"|leaders\s+relevant\s+to\s+this\s+article:?|voice\s+your\s+opinion!?"
    r"|comment\s+about\s+the\s+article|related\s+(?:content|videos)|join\s+the\s+conversation"
    r"|more\s+top\s+reads\s+from\s+.*|\(reporting\s+by\b.*"
    # A reader's comment under the story ("<name> <text>Reply", Tom's Hardware).
    r"|(?-i:.{5,300}[a-z.!?)”\"]Reply))$"
    r"|</?(?:p|a|div|img|span|script|iframe|figure|h[1-6])\b[^>]*>",
    re.IGNORECASE,
)
#: Feed items that are advertising, not stories: sportsbook promo codes in
#: a sports feed, shopping guides in a top-stories feed (2026-10-14).
_PROMO = re.compile(r"\bpromo\s+code\b|\bbonus\s+bets?\b|^deals?:", re.IGNORECASE)
_PROMO_LINK = re.compile(r"/(?:select/)?shopping/|/deals/", re.IGNORECASE)


def tidy_title(title: str, site: str = "") -> str:
    """The headline without the publisher tacked on ("… - Engadget")."""
    title = " ".join(_visible(title).split())
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
    text = _visible(text).replace("[email protected]", "(email hidden)")
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
        if len(kept) < 2 and _BYLINE.match(line) and len(line.split()) <= 9:
            continue
        if not kept and (
            # A category or caption label over the story ("Economy",
            # "President Bola Tinubu"): up to three words, no full stop,
            # then the story itself.
            len(line.split()) <= 3 and not line.endswith((".", "!", "?", "”", '"'))
            and len(following.split()) >= 5
        ):
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


#: Wording a paywall or login wall leaves in the text the extractor kept.
_PAYWALL = re.compile(
    r"subscribe to (?:continue|read|unlock)|subscribers only|for subscribers"
    r"|sign in to (?:read|continue)|create (?:a free )?account to (?:read|continue)"
    r"|already a subscriber|(?:article|story) is (?:for|available to) (?:paid )?(?:subscribers|members)"
    r"|unlock this (?:article|story)|premium content|(?:reached|used) your (?:free|monthly)"
    r"|become a (?:paid )?(?:member|subscriber)|join (?:now )?to (?:read|continue)"
    r"|members[- ]only|log in to (?:read|continue)|register (?:for free )?to (?:read|continue)",
    re.IGNORECASE,
)


def is_readable(text: str) -> bool:
    """Is this the story itself, whole? Long enough to be one (a video hub,
    a login wall and a stub are all short), and not ending in — or, if
    short, containing — the wording of a paywall."""
    size = max(len(text.split()), len(text) // 6)
    if size < MIN_ARTICLE_WORDS:
        return False
    return not (_PAYWALL.search(text[-500:]) or (size < 300 and _PAYWALL.search(text)))


FetchFeed = Callable[[str, str], list[Headline]]
FetchArticle = Callable[[str], tuple[str, str]]

#: What fetching one story's page found.
_READ = "read"  # the whole story, tidied: listable
_UNREADABLE = "unreadable"  # the page answered but gave no story (paywall, video hub, a stub)
_TRANSIENT = "transient"  # the network, a timeout or a rate limit: says nothing about the page


@dataclass(frozen=True, slots=True)
class _Article:
    """One story's page, read once when the list is built. The list only
    holds stories that came back `_READ`, so a row is a promise that it
    opens; opening it then costs nothing, and works offline."""

    title: str
    text: str
    status: str
    at: float = 0.0


@dataclass(slots=True)
class FeedHealth:
    """How one feed has behaved lately; in memory, so a restart starts clean."""

    #: Fetches in a row that failed outright.
    failures: int = 0
    #: Stories checked since one last read in full, none of which did.
    unreadable: int = 0
    benched_until: float = 0.0
    reason: str = ""


class _Gates:
    """How many story pages one pass reads at once, in all and per publisher
    (phys.org answers a burst from one address with 429), and the moment
    it stops starting new reads."""

    def __init__(self, deadline: float) -> None:
        self.pool = asyncio.Semaphore(VERIFY_WORKERS)
        self._hosts: dict[str, asyncio.Semaphore] = {}
        self._deadline = deadline

    def host(self, url: str) -> asyncio.Semaphore:
        name = urllib.parse.urlsplit(url).hostname or ""
        return self._hosts.setdefault(name, asyncio.Semaphore(VERIFY_PER_HOST))

    def expired(self) -> bool:
        return time.monotonic() >= self._deadline


async def _gather_shielded(coroutines: list[Any]) -> list[Any]:
    """gather() whose worker threads always finish before the caller is
    released, even when it is cancelled: urllib reads cannot be cancelled,
    so a superseded pass must wait for them (and discard what they found)
    rather than let two passes overlap."""
    running = asyncio.gather(*coroutines, return_exceptions=True)
    try:
        return await asyncio.shield(running)
    except asyncio.CancelledError:
        while not running.done():
            try:
                await asyncio.shield(running)
            except asyncio.CancelledError:
                continue
        raise


def _blames_the_page(exc: BaseException) -> bool:
    """Does this failure say the publisher will not give us the story — a
    4xx other than "slow down" — rather than the network being flaky?"""
    if isinstance(exc, urllib.error.HTTPError):
        return 400 <= exc.code < 500 and exc.code not in (408, 425, 429)
    return isinstance(exc, ValueError)


def _trim_lead(text: str, title: str) -> str:
    """Drop a first paragraph that is the feed's wording of the headline,
    above the page's own (BBC, 2026-10-14): the template shows one headline."""
    lead, _, rest = text.partition("\n\n")
    if rest and (_key(lead) == _key(title) or _restates(lead, title)):
        return rest
    return text


def _pick(queue: list[Headline], count: int, feed_cap: int, taken: dict[str, int]) -> list[Headline]:
    """Up to `count` stories off the front of `queue`, newest first, taking
    at most `feed_cap` from any one feed while another feed has stories
    left, so one chatty publisher does not fill a subject."""
    chosen: list[int] = []
    for relaxed in (False, True):
        for index, story in enumerate(queue):
            if len(chosen) >= count:
                break
            if index in chosen or (not relaxed and taken.get(story.feed, 0) >= feed_cap):
                continue
            chosen.append(index)
            taken[story.feed] = taken.get(story.feed, 0) + 1
    picked = [queue[index] for index in sorted(chosen)]
    queue[:] = [story for index, story in enumerate(queue) if index not in chosen]
    return picked


class NewsService:
    """Owns the headlines: polls the feeds, reads each story in full before
    it is listed, serves the tab, opens a story."""

    tab = "news"

    def __init__(
        self,
        config_path: Path = DEFAULT_CONFIG_PATH,
        *,
        fetch_feed: FetchFeed = fetch_feed,
        fetch_article: FetchArticle = fetch_article_text,
        clock: Callable[[], float] = time.time,
        max_headlines: int = MAX_HEADLINES,
        verify_deadline_s: float | None = None,
    ) -> None:
        self._config_path = config_path
        self._fetch_feed = fetch_feed
        self._fetch_article = fetch_article
        self._clock = clock
        self._max = max_headlines
        self._verify_deadline_s = verify_deadline_s
        self._config = NewsConfig.load(config_path)
        self._config_mtime = self._mtime()
        self._headlines: list[Headline] = []
        self._fetched_at: float | None = None
        self._last_error: str | None = None
        #: The newest pass found stories but none could be read in full.
        self._no_readable = False
        #: The newest pass ran out of time before the list was full.
        self._incomplete = False
        #: Link → what its page gave. Every listed story is in here, read.
        self._articles: dict[str, _Article] = {}
        #: Feed URL → how it has behaved; benched feeds are not polled.
        self._health: dict[str, FeedHealth] = {}
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
    def benched_feeds(self) -> tuple[tuple[str, str], ...]:
        """(feed URL, why) for each feed not being polled right now."""
        now = self._clock()
        return tuple(
            (url, health.reason) for url, health in self._health.items() if health.benched_until > now
        )

    async def refresh_once(self, *, force: bool = False) -> int:
        """Fetch every configured feed, each on its own thread, each failing
        alone, then read each story that would be listed. Returns how many
        headlines the tab now holds. `force` (the wearer asked) tries the
        benched feeds again too."""
        await self.refresh_changed(force=force)
        return len(self._headlines)

    async def refresh_changed(self, *, force: bool = False) -> bool:
        async with self._refresh_lock:
            return await self._refresh_changed(force)

    async def _refresh_changed(self, force: bool) -> bool:
        """One pass; True when what the wearer would see changed.

        The audit's N1: a pass that kept every feed's old headlines still
        published, and the plugin stamps "updated now" on every push — so a
        failed refresh looked fresh. A pass reports whether the headlines
        OR the failure state changed, and only that publishes.

        2026-10-15: a headline is listed only after its article was fetched
        and read in full here. A feed that is down, that blocks us, or whose
        stories are paywalled or stubs therefore contributes nothing, rather
        than a row that opens to a summary or an apology.
        """
        started = time.monotonic()
        config = self.config
        feeds = config.feeds()
        now = self._clock()
        if force:
            for health in self._health.values():
                health.failures = health.unreadable = 0
                health.benched_until = 0.0
        live = [(category, url) for category, url in feeds if not self._benched(url, now)]
        # A catalog feed's rows carry the publisher's catalog name, not the
        # feed's own title ("World", "All Top News --…", "Latest from Live…").
        names = {source.url: source.name for source in config.enabled_sources()}
        results = await _gather_shielded(
            [asyncio.to_thread(self._fetch_feed, url, category) for category, url in live]
        )
        candidates: list[Headline] = []
        failed: list[str] = []
        for (category, url), result in zip(live, results, strict=True):
            if isinstance(result, BaseException):
                failed.append(url)
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
            self._health.setdefault(url, FeedHealth()).failures = 0
            name = names.get(url)
            candidates.extend(replace(h, source=name or h.source, feed=url) for h in result)
        if live and len(failed) < len(live):
            # Some answered, some did not: those that did not are at fault.
            # (Every feed failing at once is the network, not the feeds.)
            for url in failed:
                self._feed_failed(url, now)
        # Nothing answered — or everything left is benched: keep the list.
        all_failed = bool(feeds) and len(failed) == len(live)
        before = (tuple(h.guid for h in self._headlines), self._last_error, self._no_readable)
        if all_failed:
            self._last_error = "feeds unavailable"
            self._incomplete = False
        else:
            deadline_s = self._verify_deadline_s
            if deadline_s is None:
                deadline_s = FIRST_PASS_DEADLINE_S if force or not self._headlines else VERIFY_DEADLINE_S
            by_subject = _group_by_subject(candidates, config, now)
            listed, complete = await self._select(by_subject, deadline_s, now)
            self._incomplete = not complete
            if listed or complete or not self._headlines:
                self._headlines = listed
                self._no_readable = bool(candidates) and not listed and complete
                self._fetched_at = self._clock()
                self._last_error = None
                self._prune({story.link for stories in by_subject.values() for story in stories}, now)
            # else: out of time with nothing read; the wearer keeps their list.
        after = (tuple(h.guid for h in self._headlines), self._last_error, self._no_readable)
        log.info(
            "news pass feeds=%d answered=%d benched=%d listed=%d incomplete=%s in %.1fs",
            len(feeds), len(live) - len(failed), len(feeds) - len(live),
            len(self._headlines), self._incomplete, time.monotonic() - started,
        )
        return before != after

    async def refresh(self) -> None:
        await self.refresh_once()

    async def run(self, on_refreshed: Callable[[], Any] | None = None) -> None:
        """Poll on the configured cadence, publishing only when a pass changed
        what the wearer would see (the plugin dates the panel by its push).
        A pass that ran out of time reading stories is followed a minute
        later by another, which finds the first's reads already cached."""
        self._running = True
        first_pass = True
        catch_ups = 0
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
            if self._incomplete and catch_ups < MAX_CATCH_UPS:
                catch_ups += 1
                await asyncio.sleep(CATCH_UP_DELAY_S)
            else:
                catch_ups = 0
                await asyncio.sleep(self.config.refresh_minutes * 60)

    def stop(self) -> None:
        self._running = False

    # ── Choosing and reading the list ────────────────────────────────────

    def _targets(self, subjects: list[str], kept: dict[str, list[Headline]], exhausted: set[str]) -> dict[str, int]:
        """How many stories each subject that can still grow should hold: the
        list split evenly after the subjects that ran dry keep what they have."""
        held = sum(len(kept[subject]) for subject in exhausted)
        live = [subject for subject in subjects if subject not in exhausted]
        if not live:
            return {}
        share, extra = divmod(max(0, self._max - held), len(live))
        return {subject: share + (1 if position < extra else 0) for position, subject in enumerate(live)}

    async def _select(
        self, by_subject: dict[str, list[Headline]], deadline_s: float, now: float,
    ) -> tuple[list[Headline], bool]:
        """The list: each subject's newest stories that read in full, the
        subjects shared out evenly (a subject with fewer gives its places to
        the rest), shown newest first. Returns it and whether it is as full
        as the stories allow (False: the clock ran out first)."""
        subjects = list(by_subject)
        if not subjects:
            return [], True
        queues = {subject: list(stories) for subject, stories in by_subject.items()}
        kept: dict[str, list[Headline]] = {subject: [] for subject in subjects}
        taken: dict[str, dict[str, int]] = {subject: {} for subject in subjects}
        tried = dict.fromkeys(subjects, 0)
        share = -(-self._max // len(subjects))
        attempts = max(MIN_ATTEMPTS, ATTEMPT_FACTOR * share)
        feed_cap = max(2, -(-share // 3))
        exhausted: set[str] = set()
        gates = _Gates(time.monotonic() + deadline_s)
        complete = True
        while len(exhausted) < len(subjects):
            for subject in subjects:
                # A feed benched during this pass is not worth another fetch.
                queues[subject] = [story for story in queues[subject] if not self._benched(story.feed, now)]
            targets = self._targets(subjects, kept, exhausted)
            ran_dry = [
                subject for subject, target in targets.items()
                if target > len(kept[subject]) and (tried[subject] >= attempts or not queues[subject])
            ]
            if ran_dry:
                exhausted.update(ran_dry)
                continue
            picks: list[tuple[str, Headline]] = []
            for subject, target in targets.items():
                want = min(target - len(kept[subject]), attempts - tried[subject])
                if want > 0:
                    picked = _pick(queues[subject], want, feed_cap, taken[subject])
                    tried[subject] += len(picked)
                    picks.extend((subject, story) for story in picked)
            if not picks:
                break
            outcomes = await self._read_wave([story for _, story in picks], gates)
            for (subject, story), outcome in zip(picks, outcomes, strict=True):
                if not isinstance(outcome, tuple):
                    # Not started before the deadline, or the worker blew up.
                    taken[subject][story.feed] -= 1
                    if outcome is None:
                        complete = False
                    else:
                        log.warning("reading a story failed (%s)", type(outcome).__name__)
                    continue
                article, fresh = outcome
                if fresh:
                    self._remember(story.link, article, now)
                self._note_read(story.feed, article, fresh, now)
                if article.status == _READ:
                    kept[subject].append(story)
                else:
                    taken[subject][story.feed] -= 1
            if not complete:
                break
        listed = [story for stories in kept.values() for story in stories]
        listed.sort(key=lambda story: story.published_at, reverse=True)
        return listed[: self._max], complete

    async def _read_wave(self, stories: list[Headline], gates: _Gates) -> list[Any]:
        """Read these stories' pages, a few at a time. Each result is
        (article, fetched-just-now), None when the deadline came first, or
        the exception a worker raised."""

        async def read(story: Headline) -> tuple[_Article, bool] | None:
            cached = self._cached(story.link)
            if cached is not None:
                return cached, False
            async with gates.host(story.link), gates.pool:
                if gates.expired():
                    return None
                return await asyncio.to_thread(self._read_article, story), True

        return await _gather_shielded([read(story) for story in stories])

    def _read_article(self, story: Headline) -> _Article:
        """Synchronous, on a thread: fetch one story's page and judge it."""
        try:
            title, text = self._fetch_article(story.link)
        except Exception as exc:  # noqa: BLE001 — whatever went wrong, the story is not readable
            return _Article("", "", _UNREADABLE if _blames_the_page(exc) else _TRANSIENT)
        text = _trim_lead(text, story.title)
        return _Article(title, text, _READ if is_readable(text) else _UNREADABLE)

    def _cached(self, link: str) -> _Article | None:
        article = self._articles.get(link)
        if article is None:
            return None
        if article.status == _READ or self._clock() - article.at < BAD_ARTICLE_TTL_S:
            return article
        return None

    def _remember(self, link: str, article: _Article, now: float) -> None:
        # A flaky network says nothing about the page: try again next pass.
        if article.status != _TRANSIENT:
            self._articles[link] = replace(article, at=now)

    def _prune(self, live_links: set[str], now: float) -> None:
        """Keep what the list and its candidates need: read stories still in
        a feed, and recent judgements that a page is not readable."""
        for link, article in list(self._articles.items()):
            keep = link in live_links if article.status == _READ else now - article.at < BAD_ARTICLE_TTL_S
            if not keep:
                del self._articles[link]
        overflow = len(self._articles) - MAX_CACHED_ARTICLES
        if overflow > 0:
            for link in sorted(self._articles, key=lambda key: self._articles[key].at)[:overflow]:
                del self._articles[link]

    # ── Feed health ──────────────────────────────────────────────────────

    def _benched(self, url: str, now: float) -> bool:
        health = self._health.get(url)
        return health is not None and health.benched_until > now

    def _feed_failed(self, url: str, now: float) -> None:
        """A feed that failed while others answered. Three in a row and it
        sits out an hour, then two, four… up to twelve."""
        health = self._health.setdefault(url, FeedHealth())
        health.failures += 1
        if health.failures >= FEED_FAILURES_TO_BENCH:
            wait = min(FEED_BENCH_MAX_S, FEED_BENCH_BASE_S * 2 ** (health.failures - FEED_FAILURES_TO_BENCH))
            health.benched_until = now + wait
            health.reason = f"unavailable ({health.failures} failures in a row)"
            log.info(
                "feed benched for %dm host=%s (%s)", wait // 60,
                urllib.parse.urlsplit(url).hostname or "?", health.reason,
            )

    def _note_read(self, url: str, article: _Article, fresh: bool, now: float) -> None:
        """A story from this feed was just fetched. Six in a row that cannot
        be read in full, none that can: the feed sits out a day (its pages
        are walled, blocked, or video hubs; more of them will not change that)."""
        if not fresh or not url or article.status == _TRANSIENT:
            return
        health = self._health.setdefault(url, FeedHealth())
        if article.status == _READ:
            health.unreadable = 0
            return
        health.unreadable += 1
        if health.unreadable >= FEED_UNREADABLE_TO_BENCH:
            health.benched_until = now + FEED_UNREADABLE_BENCH_S
            health.reason = "its stories cannot be read in full"
            health.unreadable = 0
            log.info("feed benched for 24h host=%s (%s)", urllib.parse.urlsplit(url).hostname or "?", health.reason)

    # ── The tab ──────────────────────────────────────────────────────────

    def snapshot(self) -> PanelSnapshot:
        now = self._clock()
        if not self._headlines:
            if self._last_error:
                empty = "Feeds unavailable — hold to retry."
            elif self._no_readable:
                empty = "No stories readable in full right now — hold to retry."
            else:
                empty = "No headlines yet — long-press to refresh."
            return PanelSnapshot(rows=(), empty=empty)
        rows = tuple(h.row(now) for h in self._headlines)
        # A kept-but-stale list says so on its own first row: the plugin's
        # "updated now" is when it was pushed, not when these were fetched.
        if self._last_error and self._fetched_at is not None:
            age = relative_age(self._fetched_at, now)
            return PanelSnapshot(rows=(f"[!] Feeds unavailable · showing headlines from {age} ago", *rows))
        return PanelSnapshot(rows=rows)

    @property
    def status_rows(self) -> int:
        """Rows the tab shows above its headlines: the "[!]" feed status.
        A tap's row index counts them; a headline index does not."""
        if not self._headlines:
            return 0
        return 1 if (self._last_error and self._fetched_at is not None) else 0

    async def open(self, index: int) -> Reader | None:
        """Open headline `index` (not a tab row: see `status_rows`)."""
        if not 0 <= index < len(self._headlines):
            return None
        headline = self._headlines[index]
        article = self._articles.get(headline.link)
        if article is not None and article.status == _READ:
            # Read when the list was built: nothing to fetch, nothing to fail.
            title, text = article.title, article.text
        else:
            try:
                title, text = await asyncio.to_thread(self._fetch_article, headline.link)
            except (OSError, ValueError, urllib.error.URLError) as exc:
                log.info("article unavailable (%s)", type(exc).__name__)
                title, text = headline.title, ""
            text = _trim_lead(text, headline.title)
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


def _group_by_subject(candidates: list[Headline], config: NewsConfig, now: float) -> dict[str, list[Headline]]:
    """One row per story across every feed (a story two subjects carry
    stays in the first that has it), newest first, grouped by subject in
    the order the wearer's choice lists them, custom feeds last."""
    by_subject: dict[str, list[Headline]] = {subject: [] for subject in (*config.categories, "custom")}
    for story in _dedupe(candidates, now):
        by_subject.setdefault(story.category, []).append(story)
    return {subject: stories for subject, stories in by_subject.items() if stories}


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
            headline = replace(headline, published_at=now)
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
