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

from ..services.news import NewsService
from ..services.panels import PanelPublisher
from ..services.pomodoro import PomodoroService
from ..services.search import SearchService
from .tools import Tool


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
            "optionally filtered by a topic word. Instant; reads a cache."
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
