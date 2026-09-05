"""Voice for the panel tabs: news, pomodoro, search — deterministic first.

"What's in the news" is answered from the cache in the time it takes to
say it; "refresh the news" fetches; "start a pomodoro" flips the timer;
"look up X" searches. None of these need the model: they are exact verbs
with exact effects, so they sit in the command chain ahead of inference,
the way "show my messages" already does. Each one also repaints the
matching tab on the glass, so the wearer hears the answer and sees it.
"""

from __future__ import annotations

import re

from .news import NewsService
from .panels import PanelPublisher
from .pomodoro import PomodoroService
from .search import SearchService

_ORDINALS = {
    "first": 0, "1st": 0, "one": 0,
    "second": 1, "2nd": 1, "two": 1,
    "third": 2, "3rd": 2, "three": 2,
    "fourth": 3, "4th": 3, "four": 3,
    "fifth": 4, "5th": 4, "five": 4,
    "sixth": 5, "6th": 5, "six": 5,
    "seventh": 6, "7th": 6, "seven": 6,
    "eighth": 7, "8th": 7, "eight": 7,
    "ninth": 8, "9th": 8, "nine": 8,
    "tenth": 9, "10th": 9, "ten": 9,
    "last": -1,
}
_ORDINAL = r"(?P<ordinal>first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last|\d{1,2}(?:st|nd|rd|th)?|one|two|three|four|five|six|seven|eight|nine|ten)"


def _normalize(text: str) -> str:
    # Whisper punctuates short utterances ("Refresh the news."). Terminal
    # punctuation is STT noise for a deterministic verb.
    return " ".join(text.strip().split()).rstrip(".,!?").strip().lower()


def _ordinal_index(token: str) -> int | None:
    token = token.lower()
    if token in _ORDINALS:
        return _ORDINALS[token]
    digits = re.match(r"^(\d{1,2})", token)
    if digits:
        return int(digits.group(1)) - 1
    return None


_NEWS_TELL = re.compile(
    r"^(?:what(?:'s| is| are)(?: in)? the news|any news|(?:read|tell) me the news|"
    r"(?:read|tell me) the headlines|(?:what are|read) the (?:latest )?headlines|"
    r"(?:the )?latest news|news headlines|what(?:'s| is) (?:the )?(?:latest|new)(?: in the news)?)$"
)
_NEWS_REFRESH = re.compile(r"^(?:refresh|update|check|reload) (?:the |my )?(?:news|headlines|feeds?)$")
_NEWS_OPEN = re.compile(r"^(?:open|show)(?: me)? (?:the |my )?(?:news|headlines)$")
_NEWS_READ_NTH = re.compile(
    rf"^(?:read|open)(?: me)? (?:the )?{_ORDINAL} (?:story|headline|article|one)$"
)


class NewsCommands:
    """"What's in the news", "refresh the news", "read the second story"."""

    def __init__(self, news: NewsService, panels: PanelPublisher) -> None:
        self._news = news
        self._panels = panels

    async def handle_command(self, text: str) -> str | None:
        normalized = _normalize(text)
        if _NEWS_OPEN.fullmatch(normalized):
            await self._panels.activate("news")
            return "Opening the news."
        if _NEWS_TELL.fullmatch(normalized):
            # From the cache, in the time it takes to say it. The tab comes
            # up under the words so the wearer can read along.
            await self._panels.activate("news")
            return self._news.spoken_headlines()
        if _NEWS_REFRESH.fullmatch(normalized):
            count = await self._news.refresh_once()
            await self._panels.activate("news")
            if count == 0:
                return "I couldn't get any headlines right now."
            # Say what actually happened (the audit's N1): a pass that kept
            # earlier headlines because every feed failed is not an update.
            if self._news.last_error:
                return (
                    f"The feeds didn't answer, so these are the {count} headlines from "
                    f"earlier. {self._news.spoken_headlines(3)}"
                )
            if self._news.partial:
                return (
                    f"Some feeds didn't answer. {count} headlines from the rest. "
                    f"{self._news.spoken_headlines(3)}"
                )
            return f"News updated: {count} headlines. {self._news.spoken_headlines(3)}"
        match = _NEWS_READ_NTH.fullmatch(normalized)
        if match:
            index = _ordinal_index(match.group("ordinal"))
            if index is None:
                return None
            if index < 0:
                index = len(self._news.headlines) - 1
            headline = self._news.headline_at(index)
            if headline is None:
                return "There is no story at that number."
            reader = await self._panels.open_by_voice("news", index)
            if reader is None:
                return f"I couldn't open {headline.title}."
            return f"Opening {headline.title}, from {headline.source}."
        return None


_POMODORO_START = re.compile(
    r"^(?:start|begin|run)(?: a| my| the)? (?:pomodoro|focus(?: block| session| timer)?|timer)$"
)
_POMODORO_PAUSE = re.compile(r"^pause(?: the| my)? (?:pomodoro|timer|focus)$|^pause$")
_POMODORO_RESUME = re.compile(r"^(?:resume|continue|unpause)(?: the| my)? (?:pomodoro|timer|focus)$|^resume$")
_POMODORO_SKIP = re.compile(
    r"^skip(?: the| this| my)? (?:break|pomodoro|block|focus)$|^(?:end|finish)(?: the| this| my)? (?:break|block) (?:now|early)$"
)
_POMODORO_RESET = re.compile(
    r"^(?:stop|reset|cancel|end)(?: the| my)? (?:pomodoro|timer|focus(?: block| session)?)$"
)
_POMODORO_OPEN = re.compile(r"^(?:open|show)(?: me)? (?:the |my )?(?:pomodoro|timer|focus timer)$")
_POMODORO_STATUS = re.compile(
    r"^(?:how (?:long|much time) is left|how long (?:do i have )?left|time left|"
    r"how(?:'s| is) (?:the |my )?(?:pomodoro|timer)|pomodoro status|timer status|"
    r"how long is left on (?:the |my )?(?:pomodoro|timer))$"
)


class PomodoroCommands:
    """Start, pause, resume, skip, reset, and "how long is left"."""

    def __init__(self, pomodoro: PomodoroService, panels: PanelPublisher) -> None:
        self._pomodoro = pomodoro
        self._panels = panels

    async def handle_command(self, text: str) -> str | None:
        normalized = _normalize(text)
        if _POMODORO_OPEN.fullmatch(normalized):
            await self._panels.activate("pomodoro")
            return "Opening the timer. " + self._pomodoro.status_text()
        if _POMODORO_START.fullmatch(normalized):
            reply = self._pomodoro.start()
            await self._panels.activate("pomodoro")
            return reply
        if _POMODORO_PAUSE.fullmatch(normalized):
            if self._pomodoro.phase == "idle" and not self._pomodoro.recovery_unavailable:
                return None if normalized == "pause" else "Nothing is running."
            return self._pomodoro.pause()
        if _POMODORO_RESUME.fullmatch(normalized):
            if self._pomodoro.phase == "idle" and not self._pomodoro.recovery_unavailable:
                return None if normalized == "resume" else self._pomodoro.start()
            return self._pomodoro.resume()
        if _POMODORO_SKIP.fullmatch(normalized):
            if self._pomodoro.phase == "idle" and not self._pomodoro.recovery_unavailable:
                return "Nothing is running."
            return self._pomodoro.skip()
        if _POMODORO_RESET.fullmatch(normalized):
            if self._pomodoro.phase == "idle" and not self._pomodoro.recovery_unavailable:
                return "No pomodoro is running."
            return self._pomodoro.reset()
        if _POMODORO_STATUS.fullmatch(normalized):
            return self._pomodoro.status_text()
        return None


# "google" only as a verb with its preposition: bare "google …" swallowed
# every sentence that merely began with the company's name ("Google Maps
# is not working") and sent it to a search engine instead of to JOAT.
_SEARCH = re.compile(
    r"^(?:look up|search(?: the web)?(?: for)?|google (?:for|search for)|web search(?: for)?|"
    r"find out about|what does the internet say about) (?P<query>.{2,120})$"
)
_SEARCH_OPEN_NTH = re.compile(rf"^(?:open|read)(?: me)? (?:the )?{_ORDINAL} (?:result|link|hit)$")
_SEARCH_OPEN = re.compile(r"^(?:open|show)(?: me)? (?:the |my )?(?:search|search results|results)$")


class SearchCommands:
    """"Look up X", "search for X", "open the second result"."""

    def __init__(self, search: SearchService, panels: PanelPublisher) -> None:
        self._search = search
        self._panels = panels

    async def handle_command(self, text: str) -> str | None:
        normalized = _normalize(text)
        if _SEARCH_OPEN.fullmatch(normalized):
            await self._panels.activate("search")
            return "Opening search. " + self._search.spoken_results(2)
        match = _SEARCH.fullmatch(normalized)
        if match:
            # Keep the wearer's own casing for the query: the normalizer
            # lower-cased it for matching only.
            query = text.strip().rstrip(".,!?")
            query = query[len(query) - len(match.group("query")):]
            spoken = await self._search.lookup(query)
            await self._panels.activate("search")
            return spoken
        nth = _SEARCH_OPEN_NTH.fullmatch(normalized)
        if nth:
            index = _ordinal_index(nth.group("ordinal"))
            if index is None:
                return None
            if index < 0:
                index = len(self._search.results) - 1
            result = self._search.result_at(index)
            if result is None:
                return "There is no result at that number."
            reader = await self._panels.open_by_voice("search", index)
            if reader is None:
                return f"I couldn't open {result.title}."
            return f"Opening {result.title}, from {result.domain}."
        return None
