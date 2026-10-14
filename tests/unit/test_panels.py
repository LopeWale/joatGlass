"""The overview's tabbed panel, Mac side: pushes, taps, refresh, paging."""

from __future__ import annotations

from typing import Any

import pytest

from joat.core.protocol import READER_BODY_MAX_UNITS, DownlinkMessage, HudList, HudPanel
from joat.services.panels import (
    READER_HINT,
    PanelPublisher,
    PanelSnapshot,
    Reader,
    paginate,
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
        # The whole article in one message, read on the glass as one
        # continuous document: the metadata, then every page's words.
        assert page.items == []
        assert page.body == "BBC · 1m · A\npage one wordspage two wordspage three"
        assert page.hint == "left x2: back"

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

    async def test_a_reader_is_sent_once_and_never_paged(self) -> None:
        transport = Transport()
        publisher, _ = panels(transport)
        await publisher.publish("news")
        await publisher.select("NEWS", 0, "panel-news-1")
        readers = [m for m in transport.sent if isinstance(m, HudList)]
        assert len(readers) == 1
        assert not hasattr(publisher, "page")


class TestPagination:
    def test_pages_are_exact_source_spans_of_the_prepared_rows(self) -> None:
        # One page model: 62 columns x 10 prepared rows (services/reader.py).
        # The per-call character budget was retired 2026-09-05 — no
        # production caller ever passed one.
        text = " ".join(f"word{i}" for i in range(300))
        pages = paginate(text)
        assert len(pages) > 1
        assert "".join(pages) == text
        assert paginate("   ") == ("   ",)
        assert paginate("") == ("No text is available for this page.",)

    def test_the_footer_promises_no_mac_page(self) -> None:
        # The plugin moves through the text and names the swipe itself.
        assert READER_HINT == "left x2: back"

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
        # No list rows at all: the text rides in `body`, whole.
        assert wire["items"] == []
        assert wire["body"] == "M" * 90 + "\n" + " ".join(["antidisestablishmentarianism"] * 40)

    def test_an_endless_body_is_capped_on_the_wire_without_splitting_a_pair(self) -> None:
        wire = HudList(items=[], body="🙂" * READER_BODY_MAX_UNITS).to_wire()
        assert len(wire["body"].encode("utf-16-le")) // 2 <= READER_BODY_MAX_UNITS
        assert set(wire["body"]) == {"🙂"}


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
        wire = last_list(transport).to_wire()
        assert wire["items"] == []
        assert wire["body"] == f"{label}\nbody words"

    @pytest.mark.parametrize("tab,title", [("news", "NEWS · ARTICLE"), ("search", "SEARCH · RESULT")])
    @pytest.mark.parametrize("source", [
        "x" * 440,
        "界" * 440,
        "  start\r\n\r\nsecond  line \tkeeps spaces  \r\n" * 24 + "  LATER MARKER  ",
        "🙂" * 320 + "LATER MARKER",
    ], ids=["unbroken-440", "cjk-440", "crlf-and-spaces", "emoji-320-and-tail"])
    async def test_every_source_character_survives_on_the_wire(
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
        wire = last_list(transport).to_wire()
        # Every character, in one message: nothing capped, trimmed or split.
        assert wire["body"] == "Origin · " + "🙂" * 80 + "\n" + source
        assert wire["requestId"] == f"panel-{tab}-1"
        assert wire["title"] == title

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
        body = last_list(transport).to_wire()["body"]
        assert body == "BBC\n" + "".join(original_pages)


class StatusRowProvider(Provider):
    """News with a failed feed: the "[!]" row sits above the headlines."""

    status_rows = 1

    def __init__(self) -> None:
        super().__init__(("[!] Some feeds unavailable · partial list", "[1m] A · BBC", "[2m] B · NPR"))

    async def open(self, index: int) -> Reader | None:
        self.opened.append(index)
        return Reader(tab="news", title="NEWS · ARTICLE", metadata="m", pages=("one",)) if index < 2 else None


class TestStatusRowTaps:
    """2026-10-06: with a feed down, every tap opened the story below it and
    the last row opened nothing ("tap to open the news does not work")."""

    async def test_a_tap_opens_the_headline_on_that_row(self) -> None:
        transport = Transport()
        provider = StatusRowProvider()
        publisher, _ = panels(transport, provider)
        await publisher.publish("news")
        await publisher.select("NEWS", 2, publisher.request_id("news"))  # the last row: B
        assert provider.opened == [1]
        assert last_list(transport).title == "NEWS · ARTICLE"

    async def test_the_status_row_opens_nothing(self) -> None:
        transport = Transport()
        provider = StatusRowProvider()
        publisher, _ = panels(transport, provider)
        await publisher.publish("news")
        await publisher.select("NEWS", 0, publisher.request_id("news"))
        assert provider.opened == []
        assert isinstance(transport.sent[-1], HudPanel)
