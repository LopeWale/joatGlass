"""Voice for the panel tabs: exact verbs, exact effects, no model."""

from __future__ import annotations

from typing import Any
from pathlib import Path

import pytest

from joat.core.protocol import DownlinkMessage, HudPanel
from joat.persistence.pomodoro import PomodoroStore, PomodoroCheckpoint, PomodoroStateError
from joat.persistence.store import DurableStore
from joat.services.news import Headline, NewsService
from joat.services.panel_commands import NewsCommands, PomodoroCommands, SearchCommands
from joat.services.panels import PanelPublisher
from joat.services.pomodoro import PomodoroService
from joat.services.search import SearchResult, SearchService

NOW = 1_800_000_000.0


@pytest.mark.parametrize('command', ['pause the timer', 'resume the timer', 'skip break'])
async def test_timer_recovery_commands_reach_the_unavailable_service(tmp_path: Path, command: str) -> None:
    class Unreadable(PomodoroStore):
        def load(self) -> PomodoroCheckpoint | None:
            raise PomodoroStateError('synthetic unreadable checkpoint')

    with DurableStore(tmp_path / 'timer.sqlite3') as durable:
        store = PomodoroStore(durable)
        prior = PomodoroCheckpoint('focus', True, 60_000, 60_000, 0, 1_060_000, 1_000_000)
        store.save(prior)
        service = PomodoroService(store=Unreadable(durable), clock=lambda: 1_000.0)
        commands = PomodoroCommands(service, PanelPublisher(Transport(), {'pomodoro': service}))
        try:
            assert 'recovery unavailable' in (await commands.handle_command(command) or '').lower()
            assert store.load() == prior
            assert await commands.handle_command('reset the timer') == 'Pomodoro reset.'
            assert not service.recovery_unavailable
            saved = store.load()
            assert saved is not None and saved.phase == 'idle' and not saved.running
        finally:
            await service.aclose()


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

    # One feed of the wearer's own, so rows keep the feed's own name (a
    # catalog feed's rows are named by news_sources.json).
    config = tmp_path / "news.json"
    config.write_text('{"categories": [], "customFeeds": ["https://x/feed"]}')
    return NewsService(
        config, fetch_feed=fetch,
        fetch_article=lambda url: ("T", "Article body. " * 80), clock=lambda: NOW,
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


class TestPomodoroSettings:
    def make(self) -> tuple[PomodoroService, PomodoroCommands, Transport]:
        transport = Transport()
        pomodoro = PomodoroService(clock=lambda: 1_000.0)
        return pomodoro, PomodoroCommands(pomodoro, PanelPublisher(transport, {"pomodoro": pomodoro})), transport

    @pytest.mark.parametrize("phrase,minutes", [
        ("Set a timer for 12 minutes.", 12), ("start a 10 minute timer", 10),
        ("set a timer for an hour", 60), ("time me for forty-five minutes", 45),
        ("set a timer for half an hour", 30), ("set a countdown for 2 hours", 120),
    ])
    async def test_countdown_phrases(self, phrase: str, minutes: int) -> None:
        pomodoro, commands, transport = self.make()
        assert await commands.handle_command(phrase) == f"Timer set for {minutes} minutes."
        assert pomodoro.phase == "timer" and pomodoro.state().length_ms == minutes * 60_000
        assert activated(transport) == ["pomodoro"]

    async def test_a_bare_timer_is_still_the_pomodoro(self) -> None:
        pomodoro, commands, _ = self.make()
        assert await commands.handle_command("start a timer") == "Focus block 1 of 4. 25 minutes."
        assert pomodoro.phase == "focus"

    @pytest.mark.parametrize("phrase,expected", [
        ("set focus to 40 minutes", "Focus blocks: 40 minutes."),
        ("Set the short break to 10", "Short breaks: 10 minutes."),
        ("long break time twenty minutes", "Long breaks: 20 minutes."),
        ("change focus time to half an hour", "Focus blocks: 30 minutes."),
        ("set work time to fifty", "Focus blocks: 50 minutes."),
    ])
    async def test_length_phrases(self, phrase: str, expected: str) -> None:
        _, commands, _ = self.make()
        assert await commands.handle_command(phrase) == expected

    @pytest.mark.parametrize("phrase,count", [
        ("set sessions to six", 6), ("do 3 sessions", 3), ("five sessions", 5),
        ("set the number of sessions to eight", 8),
    ])
    async def test_session_phrases(self, phrase: str, count: int) -> None:
        pomodoro, commands, _ = self.make()
        assert (await commands.handle_command(phrase) or "").startswith(f"{count} session")
        assert pomodoro.lengths.blocks_per_set == count

    @pytest.mark.parametrize("phrase", [
        "set a timer for 90 seconds", "set my alarm for 7 am", "remind me in 5 minutes",
        "set the volume to 40",
    ])
    async def test_unrelated_phrases_fall_through(self, phrase: str) -> None:
        _, commands, _ = self.make()
        assert await commands.handle_command(phrase) is None

    async def test_out_of_range_values_are_refused_in_words(self) -> None:
        _, commands, _ = self.make()
        assert await commands.handle_command("set focus to 2000 minutes") == "A length is 1 to 1440 minutes."
        assert await commands.handle_command("set sessions to thirty") == "A set is 1 to 12 sessions."


class TestPomodoroSetTool:
    async def test_the_model_sets_what_the_verbs_set_and_no_more(self) -> None:
        from joat.agents.panel_tools import pomodoro_set_tool

        pomodoro = PomodoroService(clock=lambda: 1_000.0)
        tool = pomodoro_set_tool(pomodoro)
        assert await tool.run({"setting": "focus", "value": 40}) == "Focus blocks: 40 minutes."
        assert await tool.run({"setting": "sessions", "value": 6}) == "6 sessions to a set."
        assert await tool.run({"setting": "short_break", "value": 0}) == "A length is 1 to 1440 minutes."
        assert await tool.run({"setting": "sessions", "value": 99}) == "A set is 1 to 12 sessions."
        assert (await tool.run({"setting": "volume", "value": 3})).startswith("Unknown setting.")
        assert await tool.run({"setting": "focus", "value": "forty"}) == "Give a whole number."
        assert await tool.run({"setting": "focus", "value": True}) == "Give a whole number."
        assert await tool.run({"setting": "timer", "value": 12}) == "Timer set for 12 minutes."
        assert pomodoro.phase == "timer"
        assert (pomodoro.lengths.focus_ms, pomodoro.lengths.blocks_per_set) == (40 * 60_000, 6)


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
