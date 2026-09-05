"""The overview's tabbed panel, Mac side: pushes, taps, refresh, paging."""

from __future__ import annotations

from typing import Any
import unicodedata

import pytest

from joat.core.protocol import DownlinkMessage, EventType, HudList, HudPanel
from joat.services.panels import (
    PanelPublisher,
    PanelSnapshot,
    Reader,
    paginate,
    reader_hint,
)


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


class Provider:
    """A tab with rows, a refresh counter, and one long row to open."""

    tab = "news"

    def __init__(self, rows: tuple[str, ...] = ("[1m] A · BBC", "[2m] B · NPR")) -> None:
        self.rows = rows
        self.refreshes = 0
        self.opened: list[int] = []
        self.fail_open = False

    def snapshot(self) -> PanelSnapshot:
        return PanelSnapshot(rows=self.rows, empty="No headlines yet.")

    async def refresh(self) -> None:
        self.refreshes += 1
        self.rows = ("[0m] fresh · BBC",)

    async def open(self, index: int) -> Reader | None:
        self.opened.append(index)
        if self.fail_open:
            raise OSError("no network")
        if index >= len(self.rows):
            return None
        return Reader(
            tab="news",
            title="NEWS · ARTICLE",
            metadata="BBC · 1m · A",
            pages=("page one words", "page two words", "page three"),
        )


def panels(transport: Transport, provider: Provider | None = None) -> tuple[PanelPublisher, Provider]:
    provider = provider or Provider()
    return PanelPublisher(transport, {"news": provider}), provider  # type: ignore[dict-item]


def last_panel(transport: Transport) -> HudPanel:
    message = transport.sent[-1]
    assert isinstance(message, HudPanel)
    return message


def last_list(transport: Transport) -> HudList:
    message = transport.sent[-1]
    assert isinstance(message, HudList)
    return message


class TestPublishing:
    async def test_each_push_carries_a_fresh_revision_and_the_snapshot(self) -> None:
        transport = Transport()
        publisher, _ = panels(transport)
        await publisher.publish("news")
        await publisher.publish("news")
        first, second = transport.sent
        assert isinstance(first, HudPanel) and isinstance(second, HudPanel)
        assert (first.revision, second.revision) == (1, 2)
        assert second.rows == ("[1m] A · BBC", "[2m] B · NPR")
        assert second.activate is False
        assert publisher.request_id("news") == "panel-news-2"

    async def test_a_voice_open_sets_activate(self) -> None:
        transport = Transport()
        publisher, _ = panels(transport)
        await publisher.activate("news")
        assert last_panel(transport).activate is True

    async def test_unknown_tabs_are_refused_at_construction(self) -> None:
        with pytest.raises(ValueError):
            PanelPublisher(Transport(), {"weather": Provider()})  # type: ignore[dict-item]

    async def test_publish_all_fills_a_fresh_plugin(self) -> None:
        transport = Transport()
        publisher, _ = panels(transport)
        await publisher.publish_all()
        assert [m.tab for m in transport.sent if isinstance(m, HudPanel)] == ["news"]


class TestTaps:
    async def test_a_tap_on_the_current_revision_opens_the_reader(self) -> None:
        transport = Transport()
        publisher, provider = panels(transport)
        await publisher.publish("news")
        await publisher.select("NEWS", 1, "panel-news-1")
        assert provider.opened == [1]
        page = last_list(transport)
        assert page.title == "NEWS · ARTICLE"
        assert page.request_id == "panel-news-1"
        assert page.selectable is False
        assert page.items[0] == "1/3 · BBC · 1m · A"
        assert page.items[1:] == ["page one words"]
        assert page.hint == "down more · left x2: back"

    async def test_a_stale_tap_is_dropped(self) -> None:
        transport = Transport()
        publisher, provider = panels(transport)
        await publisher.publish("news")
        await publisher.publish("news")
        await publisher.select("NEWS", 0, "panel-news-1")
        await publisher.select("NEWS", 0, "hud-request-abc")
        await publisher.select("MESSAGES", 0, "panel-news-2")
        assert provider.opened == []

    async def test_owns_recognises_a_tab_family_and_its_detail_pages(self) -> None:
        publisher, _ = panels(Transport())
        assert publisher.owns("NEWS")
        assert publisher.owns("news · article")
        assert not publisher.owns("MESSAGES")
        assert not publisher.owns(None)

    async def test_an_action_row_republishes_instead_of_opening(self) -> None:
        transport = Transport()
        publisher, provider = panels(transport)
        await publisher.publish("news")
        await publisher.select("NEWS", 5, "panel-news-1")  # out of range → None
        assert provider.opened == [5]
        assert last_panel(transport).revision == 2

    async def test_a_failed_open_republishes_rather_than_stranding_the_page(self) -> None:
        transport = Transport()
        publisher, provider = panels(transport)
        provider.fail_open = True
        await publisher.publish("news")
        await publisher.select("NEWS", 0, "panel-news-1")
        assert isinstance(transport.sent[-1], HudPanel)


class TestRefreshAndPaging:
    async def test_refresh_fetches_then_pushes_a_new_revision(self) -> None:
        transport = Transport()
        publisher, provider = panels(transport)
        await publisher.publish("news")
        await publisher.refresh("NEWS", "panel-news-1")
        assert provider.refreshes == 1
        pushed = last_panel(transport)
        assert pushed.revision == 2
        assert pushed.rows == ("[0m] fresh · BBC",)

    async def test_paging_moves_the_mac_owned_cursor_within_bounds(self) -> None:
        transport = Transport()
        publisher, _ = panels(transport)
        await publisher.publish("news")
        await publisher.select("NEWS", 0, "panel-news-1")
        await publisher.page("NEWS", EventType.SCROLL_BOTTOM, "panel-news-1")
        assert last_list(transport).items[1:] == ["page two words"]
        assert last_list(transport).hint == "up previous · down more · left x2: back"
        await publisher.page("NEWS", EventType.SCROLL_BOTTOM, "panel-news-1")
        assert last_list(transport).hint == "up previous · left x2: back"
        sent = len(transport.sent)
        await publisher.page("NEWS", EventType.SCROLL_BOTTOM, "panel-news-1")  # at the end
        await publisher.page("NEWS", EventType.SCROLL_TOP, "panel-news-9")  # stale
        assert len(transport.sent) == sent
        await publisher.page("NEWS", EventType.SCROLL_TOP, "panel-news-1")
        assert last_list(transport).items[1:] == ["page two words"]

    async def test_a_new_push_forgets_the_open_reader(self) -> None:
        transport = Transport()
        publisher, _ = panels(transport)
        await publisher.publish("news")
        await publisher.select("NEWS", 0, "panel-news-1")
        await publisher.activate("news")
        sent = len(transport.sent)
        await publisher.page("NEWS", EventType.SCROLL_BOTTOM, "panel-news-2")
        assert len(transport.sent) == sent


class TestPagination:
    def test_pages_break_on_words_and_never_exceed_the_budget(self) -> None:
        text = " ".join(f"word{i}" for i in range(300))
        pages = paginate(text, 100)
        assert all(len(page) <= 100 for page in pages)
        assert "".join(pages) == text
        assert paginate("   ") == ("   ",)
        assert paginate("") == ("No text is available for this page.",)

    def test_hints_name_only_the_pages_that_exist(self) -> None:
        assert reader_hint(0, 1) == "left x2: back"
        assert reader_hint(0, 3) == "down more · left x2: back"
        assert reader_hint(1, 3) == "up previous · down more · left x2: back"
        assert reader_hint(2, 3) == "up previous · left x2: back"

    async def test_reader_chunks_fit_the_list_caps(self) -> None:
        transport = Transport()
        provider = Provider()

        async def long_open(index: int) -> Reader:
            return Reader(
                tab="news", title="NEWS · ARTICLE", metadata="M" * 90,
                pages=(" ".join(["antidisestablishmentarianism"] * 40),),
            )

        provider.open = long_open  # type: ignore[method-assign]
        publisher = PanelPublisher(transport, {"news": provider})  # type: ignore[dict-item]
        await publisher.publish("news")
        await publisher.select("NEWS", 0, "panel-news-1")
        wire = last_list(transport).to_wire()
        assert len(wire["items"]) <= 20
        assert all(len(item) <= 64 for item in wire["items"])


def _reader_columns(text: str) -> int:
    """Independent bounds for the fixture characters, not the paginator."""
    return sum(
        0 if char in "\r\n" or unicodedata.combining(char) else
        8 if char == "\t" else
        2 if ord(char) > 0xFFFF or unicodedata.east_asian_width(char) in ("W", "F") else 1
        for char in text
    )


class TestLosslessReaders:
    @pytest.mark.parametrize("tab,metadata,label", [
        ("news", "", "Article"),
        ("search", "", "Result"),
        ("news", " \r\n\t ", "Article"),
        ("search", "\r\n  example.com\t \n Result title  ", "example.com Result title"),
    ], ids=["empty-news", "empty-search", "blank-metadata", "metadata-line-breaks"])
    async def test_metadata_always_identifies_prepared_rows(
        self, tab: str, metadata: str, label: str,
    ) -> None:
        transport = Transport()
        provider = Provider()

        async def open_source(index: int) -> Reader:
            return Reader(
                tab=tab, title=f"{tab.upper()} · READER", metadata=metadata,
                pages=("body words",),
            )

        provider.open = open_source  # type: ignore[method-assign]
        publisher = PanelPublisher(transport, {tab: provider})  # type: ignore[dict-item]
        await publisher.open_by_voice(tab, 0)
        items = last_list(transport).to_wire()["items"]
        assert items[0] == f"1/1 · {label}"
        assert items[1:] == ["body words"]

    @pytest.mark.parametrize("tab,title", [("news", "NEWS · ARTICLE"), ("search", "SEARCH · RESULT")])
    @pytest.mark.parametrize("source", [
        "x" * 440,
        "界" * 440,
        "  start\r\n\r\nsecond  line \tkeeps spaces  \r\n" * 24 + "  LATER MARKER  ",
        "🙂" * 320 + "LATER MARKER",
    ], ids=["unbroken-440", "cjk-440", "crlf-and-spaces", "emoji-320-and-tail"])
    async def test_every_source_character_survives_wire_pages_and_reverse_navigation(
        self, tab: str, title: str, source: str,
    ) -> None:
        transport = Transport()
        provider = Provider()

        async def open_source(index: int) -> Reader:
            # Providers may supply an overlong page; publication must make
            # every piece reachable rather than cap or trim it on the wire.
            return Reader(tab=tab, title=title, metadata="Origin · " + "🙂" * 80, pages=(source,))

        provider.open = open_source  # type: ignore[method-assign]
        publisher = PanelPublisher(transport, {tab: provider})  # type: ignore[dict-item]
        reader = await publisher.open_by_voice(tab, 0)
        assert reader is not None
        assert "".join(reader.pages) == source
        total = len(reader.pages)
        request_id = f"panel-{tab}-1"
        forward: list[list[str]] = []
        for index in range(total):
            if index:
                await publisher.page(title, EventType.SCROLL_BOTTOM, request_id)
            wire = last_list(transport).to_wire()
            items = wire["items"]
            assert "".join(items[1:]) == reader.pages[index]
            assert items[0].startswith(f"{index + 1}/{total} · ")
            assert 1 <= len(items[1:]) <= 10
            assert all(len(row.encode("utf-16-le")) // 2 <= 64 for row in items)
            assert all(_reader_columns(row) <= 62 for row in items)
            assert wire["requestId"] == request_id
            forward.append(items)

        assert "".join(row for page in forward for row in page[1:]) == source
        for index in range(total - 2, -1, -1):
            await publisher.page(title, EventType.SCROLL_TOP, request_id)
            assert last_list(transport).to_wire()["items"] == forward[index]
        sent = len(transport.sent)
        await publisher.page(title, EventType.SCROLL_TOP, request_id)
        assert len(transport.sent) == sent

    async def test_provider_page_boundaries_never_insert_or_remove_whitespace(self) -> None:
        transport = Transport()
        provider = Provider()
        original_pages = ("first  \r\n", "\r\n  second", "third\t  ")

        async def open_source(index: int) -> Reader:
            return Reader(tab="news", title="NEWS · ARTICLE", metadata="BBC", pages=original_pages)

        provider.open = open_source  # type: ignore[method-assign]
        publisher = PanelPublisher(transport, {"news": provider})  # type: ignore[dict-item]
        reader = await publisher.open_by_voice("news", 0)
        assert reader is not None
        displayed = []
        for index in range(len(reader.pages)):
            if index:
                await publisher.page("NEWS", EventType.SCROLL_BOTTOM, "panel-news-1")
            displayed.extend(last_list(transport).to_wire()["items"][1:])
        assert "".join(displayed) == "".join(original_pages)


class TestReaderSurvivesABackgroundRefresh:
    """The news poller bumps the tab's revision every pass. A wearer mid-
    article must keep paging; the reader answers under the revision it was
    opened with, and only a voice open (activate) closes it."""

    async def test_paging_keeps_working_after_a_routine_push(self) -> None:
        transport = Transport()
        publisher, _ = panels(transport)
        await publisher.publish("news")            # revision 1
        await publisher.select("NEWS", 0, "panel-news-1")
        await publisher.publish("news")            # the poller: revision 2
        assert last_panel(transport).revision == 2
        await publisher.page("NEWS", EventType.SCROLL_BOTTOM, "panel-news-1")
        page = last_list(transport)
        assert page.items[1:] == ["page two words"]
        assert page.request_id == "panel-news-1"  # what the plugin opened under
        # A tap on the OLD list after that push is still refused: the rows moved.
        await publisher.select("NEWS", 0, "panel-news-1")
        assert isinstance(transport.sent[-1], HudList)  # nothing new was opened

    async def test_a_voice_open_closes_the_reader(self) -> None:
        transport = Transport()
        publisher, _ = panels(transport)
        await publisher.publish("news")
        await publisher.select("NEWS", 0, "panel-news-1")
        await publisher.activate("news")
        sent = len(transport.sent)
        await publisher.page("NEWS", EventType.SCROLL_BOTTOM, "panel-news-1")
        assert len(transport.sent) == sent
