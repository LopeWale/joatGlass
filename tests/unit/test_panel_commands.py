"""Voice for the panel tabs: exact verbs, exact effects, no model."""

from __future__ import annotations

from typing import Any

import pytest

from joat.core.protocol import DownlinkMessage, HudPanel
from joat.services.news import Headline, NewsService
from joat.services.panel_commands import NewsCommands, PomodoroCommands, SearchCommands
from joat.services.panels import PanelPublisher
from joat.services.pomodoro import PomodoroService
from joat.services.search import SearchResult, SearchService

NOW = 1_800_000_000.0


class Transport:
    def __init__(self) -> None:
        self.sent: list[DownlinkMessage] = []
        self.connected = True

    async def send(self, message: DownlinkMessage) -> None:
        self.sent.append(message)

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def on_audio(self, callback: Any) -> None: ...
    def on_input(self, callback: Any) -> None: ...
    def on_device(self, callback: Any) -> None: ...


def activated(transport: Transport) -> list[str]:
    return [m.tab for m in transport.sent if isinstance(m, HudPanel) and m.activate]


def news_service(tmp_path, titles: list[str]) -> NewsService:
    def fetch(url: str, category: str) -> list[Headline]:
        return [
            Headline(title=t, link=f"https://x/{i}", source="BBC", category="top",
                     published_at=NOW - 60 * (i + 1), guid=t)
            for i, t in enumerate(titles)
        ]

    return NewsService(
        tmp_path / "news.json", fetch_feed=fetch,
        fetch_article=lambda url: ("T", "Article body. " * 50), clock=lambda: NOW,
    )


class TestNews:
    async def test_whats_in_the_news_speaks_the_cache_and_shows_the_tab(self, tmp_path) -> None:
        transport = Transport()
        news = news_service(tmp_path, ["One", "Two"])
        panels = PanelPublisher(transport, {"news": news})
        commands = NewsCommands(news, panels)
        assert await commands.handle_command("What's in the news?") == "I don't have any headlines yet."
        await news.refresh_once()
        reply = await commands.handle_command("any news")
        assert reply == "Here are the headlines. 1. One from BBC. 2. Two from BBC."
        assert activated(transport) == ["news", "news"]

    async def test_refresh_fetches_then_reports_the_count(self, tmp_path) -> None:
        transport = Transport()
        news = news_service(tmp_path, ["One", "Two", "Three"])
        commands = NewsCommands(news, PanelPublisher(transport, {"news": news}))
        reply = await commands.handle_command("Refresh the news.")
        assert reply is not None and reply.startswith("News updated: 3 headlines.")
        assert reply.count(". 3. Three") == 1

    async def test_read_the_second_story_opens_that_row(self, tmp_path) -> None:
        transport = Transport()
        news = news_service(tmp_path, ["One", "Two"])
        commands = NewsCommands(news, PanelPublisher(transport, {"news": news}))
        await news.refresh_once()
        assert await commands.handle_command("read the second story") == "Opening Two, from BBC."
        assert await commands.handle_command("open the last story") == "Opening Two, from BBC."
        assert await commands.handle_command("read the 7th article") == "There is no story at that number."
        assert activated(transport) == ["news", "news"]
        titles = [getattr(m, "title", None) for m in transport.sent]
        assert titles.count("NEWS · ARTICLE") == 2

    async def test_open_the_news_only_shows_the_tab(self, tmp_path) -> None:
        transport = Transport()
        news = news_service(tmp_path, [])
        commands = NewsCommands(news, PanelPublisher(transport, {"news": news}))
        assert await commands.handle_command("Show me the news") == "Opening the news."
        assert activated(transport) == ["news"]

    async def test_unrelated_speech_falls_through(self, tmp_path) -> None:
        news = news_service(tmp_path, [])
        commands = NewsCommands(news, PanelPublisher(Transport(), {"news": news}))
        assert await commands.handle_command("what's the weather") is None
        assert await commands.handle_command("news about my cousin") is None


class TestPomodoro:
    async def test_start_pause_resume_status_and_reset(self) -> None:
        transport = Transport()
        pomodoro = PomodoroService(clock=lambda: 1_000.0)
        commands = PomodoroCommands(pomodoro, PanelPublisher(transport, {"pomodoro": pomodoro}))
        assert await commands.handle_command("how long is left") is not None
        assert await commands.handle_command("Start a pomodoro.") == "Focus block 1 of 4. 25 minutes."
        assert activated(transport) == ["pomodoro"]
        assert await commands.handle_command("pause the timer") == "Paused. 25 minutes left in your focus block 1 of 4."
        assert await commands.handle_command("resume") == "Resumed. 25 minutes left in your focus block 1 of 4."
        assert await commands.handle_command("time left") == "25 minutes left in your focus block 1 of 4."
        assert await commands.handle_command("skip to the break") is None  # not a phrase we claim
        assert await commands.handle_command("skip the block") == "5 minutes left in your short break."
        assert await commands.handle_command("stop the pomodoro") == "Pomodoro reset."
        assert await commands.handle_command("stop the pomodoro") == "No pomodoro is running."

    async def test_bare_pause_and_resume_are_only_claimed_while_running(self) -> None:
        pomodoro = PomodoroService(clock=lambda: 1_000.0)
        commands = PomodoroCommands(pomodoro, PanelPublisher(Transport(), {"pomodoro": pomodoro}))
        assert await commands.handle_command("pause") is None
        assert await commands.handle_command("resume") is None
        assert await commands.handle_command("begin my focus session") is not None
        assert await commands.handle_command("pause") is not None

    async def test_show_the_timer_opens_the_tab_and_reports(self) -> None:
        transport = Transport()
        pomodoro = PomodoroService(clock=lambda: 1_000.0)
        commands = PomodoroCommands(pomodoro, PanelPublisher(transport, {"pomodoro": pomodoro}))
        reply = await commands.handle_command("show the timer")
        assert reply is not None and reply.startswith("Opening the timer. No pomodoro running.")
        assert activated(transport) == ["pomodoro"]


class TestSearch:
    @pytest.fixture
    def rig(self):
        transport = Transport()
        queries: list[str] = []

        def search_fn(query: str, max_results: int) -> list[SearchResult]:
            queries.append(query)
            return [SearchResult("Result One", "https://one.example/a", "About one")]

        search = SearchService(search=search_fn, fetch_page=lambda url: ("Result One", "Page body."))
        panels = PanelPublisher(transport, {"search": search})
        return SearchCommands(search, panels), transport, queries

    async def test_look_up_keeps_the_wearers_casing_and_shows_the_tab(self, rig) -> None:
        commands, transport, queries = rig
        reply = await commands.handle_command("Look up Even Realities G2.")
        assert queries == ["Even Realities G2"]
        assert reply == "For Even Realities G2, 1. Result One, one.example: About one."
        assert activated(transport) == ["search"]
        assert await commands.handle_command("google for the weather in Lagos") is not None
        assert queries[-1] == "the weather in Lagos"
        # The company's name is not a verb: this sentence is for JOAT, not a
        # search engine (it used to become a query for "maps is not working").
        assert await commands.handle_command("Google Maps is not working") is None

    async def test_open_the_first_result_reads_the_page(self, rig) -> None:
        commands, transport, _ = rig
        assert await commands.handle_command("open the first result") == "There is no result at that number."
        await commands.handle_command("search for g2")
        assert await commands.handle_command("open the first result") == "Opening Result One, from one.example."
        assert any(getattr(m, "title", None) == "SEARCH · RESULT" for m in transport.sent)

    async def test_show_search_reports_the_last_lookup(self, rig) -> None:
        commands, transport, _ = rig
        assert await commands.handle_command("show the search") == "Opening search. Nothing has been looked up yet."
        assert activated(transport) == ["search"]

    async def test_a_too_short_or_unrelated_phrase_falls_through(self, rig) -> None:
        commands, _, queries = rig
        assert await commands.handle_command("look up a") is None
        assert await commands.handle_command("what time is it") is None
        assert queries == []
