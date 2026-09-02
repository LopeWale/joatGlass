"""The overview's tabbed right panel, Mac side.

The glasses show a rail of tabs — notifications, news, pomodoro, search.
Which tab is up is the plugin's business; what stands behind news, pomodoro
and search is the Mac's. Each of those is a `PanelProvider` that can say
what its tab shows right now (no I/O), refresh itself (I/O), and open one of
its rows. This module publishes their snapshots as `hud.panel` pushes and
answers the taps that come back.

Two rules keep the glasses fast:

- Publishing is proactive. The plugin caches every push, so a swipe onto a
  tab never waits on the Mac. Only opening a row costs a round trip.
- Every push carries a revision, and a tap echoes it. A tap that lands
  after a refresh names rows the wearer no longer sees, so it is refused
  rather than opening whatever now sits at that index.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Protocol

from ..core.interfaces import GlassesTransport
from ..core.protocol import (
    PANEL_TABS,
    EventType,
    HudList,
    HudPanel,
    HudPanelPomodoro,
    panel_request_id,
    parse_panel_request_id,
)

log = logging.getLogger(__name__)

#: The focus-page title each tab expands into on the glass. A reader page
#: extends it ("NEWS · ARTICLE"), which is how the plugin's two-deep Back
#: knows the parent. Plugin counterpart: PANEL_TAB_TITLES in PanelRail.ts.
PANEL_TITLES: dict[str, str] = {
    "news": "NEWS",
    "pomodoro": "POMODORO",
    "search": "SEARCH",
}
_TITLE_TABS = {title: tab for tab, title in PANEL_TITLES.items()}

#: One reader page. The plugin re-wraps at 52 columns over ten lines
#: (MESSAGES_READER_BODY_LINE_CAPACITY); this leaves a little slack so a
#: page never spills a line the wearer cannot reach.
READER_PAGE_CHARACTERS = 440
#: HudList caps each item at 64 characters; the plugin joins the body
#: items with spaces, so a page travels as short word-bounded chunks.
_READER_CHUNK_CHARACTERS = 60


@dataclass(frozen=True, slots=True)
class PanelSnapshot:
    """What one tab shows right now. Cheap: never does I/O."""

    rows: tuple[str, ...] = ()
    headline: str | None = None
    empty: str | None = None
    pomodoro: HudPanelPomodoro | None = None


@dataclass(slots=True)
class Reader:
    """An opened row's long text, paginated, with the Mac-owned cursor."""

    tab: str
    title: str
    metadata: str
    pages: tuple[str, ...]
    index: int = 0
    #: The tab revision this reader was opened under. A background refresh
    #: bumps the tab's revision without closing the reader, so its page
    #: requests keep echoing THIS one and are answered, not refused.
    revision: int = 0


def paginate(text: str, page_characters: int = READER_PAGE_CHARACTERS) -> tuple[str, ...]:
    """Split text at word boundaries into fixed reader pages."""
    remaining = " ".join(text.split())
    if not remaining:
        return ("No text is available for this page.",)
    pages: list[str] = []
    while remaining:
        if len(remaining) <= page_characters:
            pages.append(remaining)
            break
        boundary = remaining.rfind(" ", 0, page_characters + 1)
        if boundary <= 0:
            boundary = page_characters
        pages.append(remaining[:boundary].rstrip())
        remaining = remaining[boundary:].lstrip()
    return tuple(pages)


def reader_hint(index: int, total: int) -> str:
    """The footer names only the pages that exist; the plugin turns a
    boundary swipe into a page request only when the hint offers it."""
    if total <= 1:
        return "left x2: back"
    if index == 0:
        return "down more · left x2: back"
    if index + 1 < total:
        return "up previous · down more · left x2: back"
    return "up previous · left x2: back"


class PanelProvider(Protocol):
    """One tab's content, as the Mac knows it."""

    @property
    def tab(self) -> str: ...

    def snapshot(self) -> PanelSnapshot: ...

    async def refresh(self) -> None: ...

    async def open(self, index: int) -> Reader | None:
        """Open one row. A reader for long text; None when the row was an
        action (a pomodoro control) whose effect shows in the next push."""
        ...


@dataclass(slots=True)
class _TabState:
    revision: int = 0
    reader: Reader | None = None
    #: One in-flight open per tab, so a double tap cannot fetch twice.
    opening: bool = False


class PanelPublisher:
    """Publish tab snapshots; answer the taps, refreshes, and pages they earn."""

    def __init__(
        self,
        transport: GlassesTransport,
        providers: dict[str, PanelProvider],
    ) -> None:
        unknown = set(providers) - set(PANEL_TABS)
        if unknown:
            raise ValueError(f"unknown panel tabs: {sorted(unknown)}")
        self._transport = transport
        self._providers = providers
        self._tabs: dict[str, _TabState] = {tab: _TabState() for tab in providers}

    def owns(self, name: str | None) -> bool:
        """Whether a focus-page family ("NEWS", "NEWS · ARTICLE") is a tab."""
        return self._tab_for(name) is not None

    def request_id(self, tab: str) -> str:
        return panel_request_id(tab, self._tabs[tab].revision)

    async def publish(self, tab: str, *, activate: bool = False) -> None:
        """Push one tab's current snapshot under a fresh revision."""
        provider = self._providers.get(tab)
        if provider is None:
            return
        state = self._tabs[tab]
        state.revision += 1
        if activate:
            # A new page is coming up; whatever was open under it is gone.
            state.reader = None
        snapshot = provider.snapshot()
        await self._transport.send(HudPanel(
            tab=tab,
            revision=state.revision,
            rows=snapshot.rows,
            headline=snapshot.headline,
            empty=snapshot.empty,
            pomodoro=snapshot.pomodoro,
            activate=activate,
        ))

    async def publish_all(self) -> None:
        """A plugin that just connected has an empty cache: fill it."""
        for tab in self._providers:
            await self.publish(tab)

    async def activate(self, tab: str) -> None:
        """A voice open: the plugin switches to and expands the tab."""
        await self.publish(tab, activate=True)

    async def open_by_voice(self, tab: str, index: int) -> Reader | None:
        """"Read the second story": expand the tab, then open its row."""
        await self.activate(tab)
        return await self._open(tab, index)

    async def select(self, name: str | None, index: int, request_id: str | None) -> None:
        """A row tap from the glass, carrying the revision it was made on."""
        tab = self._tab_for(name)
        if tab is None:
            return
        parsed = parse_panel_request_id(request_id)
        if parsed != (tab, self._tabs[tab].revision):
            log.info("dropping stale panel tap tab=%s request=%s", tab, request_id)
            return
        await self._open(tab, index)

    async def refresh(self, name: str | None, request_id: str | None) -> None:
        """Long press at the top of an expanded tab: fetch again, push again."""
        tab = self._tab_for(name)
        if tab is None:
            return
        parsed = parse_panel_request_id(request_id)
        if parsed is not None and parsed[0] != tab:
            return
        try:
            await self._providers[tab].refresh()
        except Exception:
            # The push still goes out: stale rows beat a page that never
            # answers a hold. The provider logged what went wrong.
            log.warning("panel refresh failed tab=%s", tab, exc_info=True)
        await self.publish(tab)

    async def page(self, name: str | None, event_type: int | None, request_id: str | None) -> None:
        """A boundary swipe inside an opened reader: move the Mac's cursor."""
        tab = self._tab_for(name)
        if tab is None:
            return
        state = self._tabs[tab]
        reader = state.reader
        if reader is None or parse_panel_request_id(request_id) != (tab, reader.revision):
            return
        if event_type == EventType.SCROLL_TOP and reader.index > 0:
            reader.index -= 1
        elif event_type == EventType.SCROLL_BOTTOM and reader.index + 1 < len(reader.pages):
            reader.index += 1
        else:
            return
        await self._send_reader(tab, reader)

    async def _open(self, tab: str, index: int) -> Reader | None:
        state = self._tabs[tab]
        if state.opening:
            return None
        state.opening = True
        try:
            reader = await self._providers[tab].open(index)
        except Exception:
            log.warning("panel open failed tab=%s index=%d", tab, index, exc_info=True)
            reader = None
        finally:
            state.opening = False
        if reader is None:
            # An action row (pomodoro), or nothing to show: the tab's next
            # snapshot is the receipt.
            await self.publish(tab)
            return None
        reader.revision = state.revision
        state.reader = reader
        await self._send_reader(tab, reader)
        return reader

    async def _send_reader(self, tab: str, reader: Reader) -> None:
        page = reader.pages[reader.index]
        total = len(reader.pages)
        metadata = reader.metadata
        if total > 1:
            metadata = f"{metadata} · {reader.index + 1}/{total}"
        await self._transport.send(HudList(
            items=[metadata[:64], *_chunks(page)],
            title=reader.title,
            hint=reader_hint(reader.index, total),
            # The plugin matches this against the id it opened the reader
            # under; a refresh since then must not orphan the page.
            request_id=panel_request_id(tab, reader.revision),
            selectable=False,
        ))

    @staticmethod
    def _tab_for(name: str | None) -> str | None:
        family = (name or "").split("·")[0].strip().upper()
        return _TITLE_TABS.get(family)


def _chunks(text: str) -> list[str]:
    """Word-bounded pieces that fit HudList's per-item cap. At most 19 so
    the metadata line and the chunks stay inside the 20-item cap."""
    words = text.split()
    chunks: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= _READER_CHUNK_CHARACTERS:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = word[:_READER_CHUNK_CHARACTERS]
    if current:
        chunks.append(current)
    return chunks[:19] or ["No text is available for this page."]




__all__ = [
    "PANEL_TITLES",
    "PanelProvider",
    "PanelPublisher",
    "PanelSnapshot",
    "Reader",
    "paginate",
    "reader_hint",
]
