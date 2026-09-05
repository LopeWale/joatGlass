"""Tools on the belt for the panel tabs: news, search, and the timer.

The command chain answers the exact verbs. These are for the questions
that need the model to notice a tool would help — "what did the second
story say?", "is there anything about Apple in the news?", "how much
longer until my break?" — and for follow-ups inside a conversation. Every
one reads the Mac's cache or runs one bounded lookup; nothing here can
hold the reply loop past the belt's ten-second ceiling.
"""

from __future__ import annotations

from typing import Any

from ..services.news import NewsService, relative_age
from ..services.panels import PanelPublisher
from ..services.pomodoro import PomodoroService
from ..services.search import SearchService
from .tools import Tool

# An article page read is a network fetch plus extraction (news.py's
# FETCH_TIMEOUT_S is 12 s), so it gets its own ceiling above the belt's
# default ten seconds. The mind's own turn timeout is 90 s.
ARTICLE_TOOL_TIMEOUT_S = 25.0
# The lens reader pages a whole article; the mind gets enough to discuss it
# without a single long feature swallowing the reply loop's context.
ARTICLE_READ_CHARS = 3_000


def news_article_tool(news: NewsService) -> Tool:
    """The article behind a cached headline, as text the mind can discuss.

    The audit's N2: the mind only ever saw headlines, so "what does that
    story say?" could not be answered. This opens the same article the
    News tab opens on a tap — same fetch, same extraction, same fallback
    when a publisher page cannot be read — and hands the mind the text.
    """

    async def run(arguments: dict[str, Any]) -> str:
        headlines = news.headlines
        if not headlines:
            return "no headlines are cached yet; the wearer can say 'refresh the news'"
        index: int | None = None
        number = arguments.get("number")
        if isinstance(number, bool):
            number = None
        if isinstance(number, int) and 1 <= number <= len(headlines):
            index = number - 1
        elif isinstance(number, str) and number.strip().isdigit():
            value = int(number.strip())
            if 1 <= value <= len(headlines):
                index = value - 1
        if index is None:
            title = str(arguments.get("title", "") or "").strip().casefold()
            if title:
                for position, headline in enumerate(headlines):
                    if title in headline.title.casefold():
                        index = position
                        break
        if index is None:
            listed = "; ".join(f"{n}. {h.title} ({h.source})" for n, h in enumerate(headlines[:10], start=1))
            return f"no cached headline matches; headlines: {listed}"
        reader = await news.open(index)
        headline = headlines[index]
        if reader is None:
            return f"the headline {headline.title!r} is no longer cached; refresh the news"
        body = "".join(reader.pages).strip()
        truncated = len(body) > ARTICLE_READ_CHARS
        if truncated:
            body = body[:ARTICLE_READ_CHARS].rsplit(" ", 1)[0] + "…"
        age = relative_age(headline.published_at)
        return (
            f"{headline.title} ({headline.source}, {age}"
            f"{', truncated for length' if truncated else ''}):\n{body}"
        )

    return Tool(
        name="read_news_article",
        description=(
            "Open one of the cached news headlines and read the article text. "
            "Call this when the wearer asks what a story says, for details on a "
            "headline, or to read an article. Pick by number from news_headlines "
            "or by a few words of the title."
        ),
        parameters={
            "type": "object",
            "properties": {
                "number": {"type": "integer", "description": "1-based position from news_headlines"},
                "title": {"type": "string", "description": "A few words of the headline"},
            },
        },
        run=run,
        timeout_s=ARTICLE_TOOL_TIMEOUT_S,
        decision_hints=("news", "article", "story", "stories", "headline", "headlines", "says"),
    )


def news_headlines_tool(news: NewsService) -> Tool:
    async def run(arguments: dict[str, Any]) -> str:
        topic = str(arguments.get("topic", "") or "").strip().lower()
        headlines = news.headlines
        if not headlines:
            return "no headlines are cached yet; the wearer can say 'refresh the news'"
        if topic:
            matches = [h for h in headlines if topic in h.title.lower() or topic == h.category]
            if not matches:
                return f"no cached headline mentions {topic!r}; headlines: " + "; ".join(
                    f"{h.title} ({h.source})" for h in headlines[:8]
                )
            headlines = tuple(matches)
        return "; ".join(f"{h.title} ({h.source}, {h.category})" for h in headlines[:10])

    return Tool(
        name="news_headlines",
        description=(
            "The latest news headlines JOAT already fetched from free RSS feeds, "
            "optionally filtered by a topic word. Instant; reads a cache. To read "
            "the article behind a headline, call read_news_article."
        ),
        parameters={
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Optional word to filter headlines by, e.g. 'apple' or 'science'.",
                },
            },
        },
        run=run,
        decision_hints=("news", "headline", "headlines", "story", "stories"),
    )


def web_search_tool(search: SearchService, panels: PanelPublisher | None = None) -> Tool:
    async def run(arguments: dict[str, Any]) -> str:
        query = str(arguments.get("query", "") or "").strip()
        if not query:
            return "error: web_search needs a query"
        spoken = await search.lookup(query)
        if panels is not None:
            await panels.activate("search")
        results = search.results
        if not results:
            return spoken
        return "; ".join(
            f"{r.title} — {r.snippet[:160]} ({r.domain})" if r.snippet else f"{r.title} ({r.domain})"
            for r in results[:5]
        )

    return Tool(
        name="web_search",
        description=(
            "Look something up on the web and get the top results with snippets. "
            "Use for facts, definitions, current events, or anything not already known."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for."},
            },
            "required": ["query"],
        },
        run=run,
        decision_hints=("look up", "search", "google", "find out", "who is", "what is"),
    )


def pomodoro_status_tool(pomodoro: PomodoroService) -> Tool:
    async def run(_arguments: dict[str, Any]) -> str:
        return pomodoro.status_text()

    return Tool(
        name="pomodoro_status",
        description="How the wearer's pomodoro focus timer stands: phase, time left, blocks done.",
        parameters={"type": "object", "properties": {}},
        run=run,
        decision_hints=("pomodoro", "timer", "focus block", "break"),
    )
