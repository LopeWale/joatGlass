"""The Search tab: "JOAT, look up …", answered from the Mac.

Key-free web search through `ddgs`, the metasearch that fronts DuckDuckGo,
Brave and Bing — the wearer's ruling was no paid API for something the open
web already answers. The Mac runs the query, keeps the results, pushes them
to the tab, and speaks the top of the list. Opening a result fetches that
page's readable text for the reader, the same path an article takes.

There is no keyboard on the glasses: the query is always spoken, or comes
from the assistant's own tool call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
import time
from typing import Any
import urllib.error

from .news import ROW_CHARACTERS, article_domain, fetch_article_text, shorten
from .panels import PanelSnapshot, Reader, paginate

log = logging.getLogger(__name__)

MAX_RESULTS = 8
#: ddgs takes whole seconds. Under the belt's ten-second ceiling.
SEARCH_TIMEOUT_S = 8


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str

    @property
    def domain(self) -> str:
        return article_domain(self.url)

    def row(self) -> str:
        """"Title · domain" — the dense-row grammar the glass parses."""
        suffix = f" · {self.domain}" if self.domain else ""
        return f"{shorten(self.title, max(12, ROW_CHARACTERS - len(suffix)))}{suffix}"


def ddgs_search(query: str, max_results: int = MAX_RESULTS) -> list[SearchResult]:
    """Synchronous metasearch. Call on a thread; it may take a few seconds."""
    from ddgs import DDGS  # lazy: optional at import time, required at call

    raw: list[dict[str, Any]] = DDGS(timeout=SEARCH_TIMEOUT_S).text(
        query, max_results=max_results, backend="auto",
    )
    results: list[SearchResult] = []
    for item in raw:
        title = " ".join(str(item.get("title", "") or "").split())
        url = str(item.get("href", "") or item.get("url", "") or "")
        if not title or not url.startswith(("http://", "https://")):
            continue
        results.append(SearchResult(
            title=title,
            url=url,
            snippet=" ".join(str(item.get("body", "") or "").split()),
        ))
    return results


SearchFn = Callable[[str, int], list[SearchResult]]
FetchPage = Callable[[str], tuple[str, str]]


#: The tap that asks for a query; the page's one control.
SEARCH_CONTROL_ROW = "⌕ Look something up"

#: ask(question, field, consume) -> published; see PromptedSpeechRuntime.ask_capability.
AskWearer = Callable[[str, str, Callable[[str], Awaitable[None]]], Awaitable[bool]]


class SearchService:
    """Owns the last lookup: its results, and the page the wearer opened."""

    tab = "search"

    def __init__(
        self,
        *,
        search: SearchFn = ddgs_search,
        fetch_page: FetchPage = fetch_article_text,
        clock: Callable[[], float] = time.time,
        max_results: int = MAX_RESULTS,
    ) -> None:
        self._search = search
        self._fetch_page = fetch_page
        self._clock = clock
        self._max = max_results
        self._ask: AskWearer | None = None
        self._after_lookup: Callable[[], Awaitable[None]] | None = None
        self._query = ""
        self._results: list[SearchResult] = []
        self._searched_at: float | None = None
        self._failed = False

    @property
    def query(self) -> str:
        return self._query

    @property
    def results(self) -> tuple[SearchResult, ...]:
        return tuple(self._results)

    def attach_prompt(
        self, ask: AskWearer, after_lookup: Callable[[], Awaitable[None]],
    ) -> None:
        """Give the tab's control row a voice: a tap asks for the query.

        The tab is a page the wearer opened by hand, so it offers a row to
        tap rather than telling them what to say; the row asks "What should I
        look up?" through the acknowledged prompt, and the answer runs the
        same lookup the spoken grammar does. `after_lookup` republishes the
        tab, which the publisher does for a tap but not for a late answer.
        """
        self._ask = ask
        self._after_lookup = after_lookup

    async def ask_for_query(self) -> bool:
        """The tapped control row: ask, then look up what was said."""
        if self._ask is None:
            return False

        async def consume(text: str) -> None:
            await self.lookup(text)
            if self._after_lookup is not None:
                await self._after_lookup()

        return await self._ask("What should I look up?", "query", consume)

    async def lookup(self, query: str) -> str:
        """Run the query; return what JOAT should say. Never raises."""
        cleaned = " ".join(query.split()).strip(" ?.!")
        if not cleaned:
            return "What should I look up?"
        self._query = cleaned
        try:
            results = await asyncio.to_thread(self._search, cleaned, self._max)
        except Exception as exc:
            log.info("search unavailable (%s)", type(exc).__name__)
            self._results = []
            self._failed = True
            self._searched_at = self._clock()
            return f"I couldn't reach a search engine for {cleaned}."
        self._results = results[: self._max]
        self._failed = False
        self._searched_at = self._clock()
        return self.spoken_results()

    def spoken_results(self, limit: int = 3) -> str:
        if not self._query:
            return "Nothing has been looked up yet."
        if not self._results:
            return f"I found nothing for {self._query}."
        parts = []
        for number, result in enumerate(self._results[:limit], start=1):
            detail = f": {shorten(result.snippet, 140)}" if result.snippet else ""
            parts.append(f"{number}. {result.title}, {result.domain}{detail}.")
        return f"For {self._query}, " + " ".join(parts)

    # ── PanelProvider ────────────────────────────────────────────────────

    def snapshot(self) -> PanelSnapshot:
        # The page is a control surface: its last row always starts a new
        # search by tap. Voice works from anywhere and needs no row.
        if not self._query:
            return PanelSnapshot(rows=(SEARCH_CONTROL_ROW,))
        if not self._results:
            headline = (
                f"{self._query} — search unavailable"
                if self._failed
                else f"{self._query} — no results"
            )
            return PanelSnapshot(rows=(SEARCH_CONTROL_ROW,), headline=headline)
        return PanelSnapshot(
            rows=tuple(r.row() for r in self._results) + (SEARCH_CONTROL_ROW,),
            headline=self._query,
        )

    async def refresh(self) -> None:
        if self._query:
            await self.lookup(self._query)

    async def open(self, index: int) -> Reader | None:
        if index == len(self._results):
            # The control row: its answer arrives later and republishes.
            await self.ask_for_query()
            return None
        if not 0 <= index < len(self._results):
            return None
        result = self._results[index]
        try:
            title, text = await asyncio.to_thread(self._fetch_page, result.url)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            log.info("result page unavailable (%s)", type(exc).__name__)
            title, text = result.title, ""
        body = text or result.snippet or "The page text could not be read on the Mac."
        return Reader(
            tab=self.tab,
            title="SEARCH · RESULT",
            metadata=f"{result.domain} · {title or result.title}",
            pages=paginate(body),
        )

    def result_at(self, index: int) -> SearchResult | None:
        return self._results[index] if 0 <= index < len(self._results) else None

