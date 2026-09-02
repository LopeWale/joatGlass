"""The overview's tabbed panel, Mac side: pushes, taps, refresh, paging."""

from __future__ import annotations

from typing import Any

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
        assert page.items[0] == "BBC · 1m · A · 1/3"
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
        assert " ".join(pages) == text
        assert paginate("   ") == ("No text is available for this page.",)

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
