"""The News tab: free feeds, fetched on the Mac, read on the glass."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from joat.services.news import (
    CATEGORY_FEEDS,
    DEFAULT_CATEGORIES,
    Headline,
    NewsConfig,
    NewsService,
    fetch_feed,
)

NOW = 1_800_000_000.0

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<title>BBC News - Technology</title>
<item><title>Apple  raises   prices</title><link>https://bbc.example/1</link>
<guid>bbc-1</guid><pubDate>Tue, 02 Sep 2026 09:00:00 GMT</pubDate></item>
<item><title>Untitled link</title><link></link></item>
<item><title>Older story</title><link>https://bbc.example/2</link>
<pubDate>Mon, 01 Sep 2026 09:00:00 GMT</pubDate></item>
</channel></rss>"""

GOOGLE = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Top stories - Google News</title>
<item><title>Apple raises prices - Reuters</title><link>https://news.google.com/rss/articles/x</link>
<pubDate>Tue, 02 Sep 2026 09:30:00 GMT</pubDate><source url="https://reuters.com">Reuters</source></item>
</channel></rss>"""


class TestConfig:
    def test_defaults_when_the_file_is_missing_or_broken(self, tmp_path: Path) -> None:
        assert NewsConfig.load(tmp_path / "none.json").categories == DEFAULT_CATEGORIES
        broken = tmp_path / "news.json"
        broken.write_text("{not json")
        assert NewsConfig.load(broken) == NewsConfig()

    def test_only_known_categories_and_http_feeds_survive(self, tmp_path: Path) -> None:
        path = tmp_path / "news.json"
        path.write_text(json.dumps({
            "categories": ["technology", "astrology", " science "],
            "customFeeds": ["https://example.org/feed.xml", "ftp://nope", 3],
            "refreshMinutes": 1,
        }))
        config = NewsConfig.load(path)
        assert config.categories == ("technology", "science")
        assert config.custom_feeds == ("https://example.org/feed.xml",)
        assert config.refresh_minutes == 5  # floor
        feeds = config.feeds()
        assert feeds[: len(CATEGORY_FEEDS["technology"])] == tuple(
            ("technology", url) for url in CATEGORY_FEEDS["technology"]
        )
        assert feeds[-1] == ("custom", "https://example.org/feed.xml")

    def test_custom_feeds_alone_are_a_complete_configuration(self) -> None:
        config = NewsConfig.from_dict({"categories": [], "customFeeds": ["https://a/b"]})
        assert config.categories == ()
        assert config.feeds() == (("custom", "https://a/b"),)
        assert config.to_dict()["customFeeds"] == ["https://a/b"]


class TestFetchFeed:
    def test_parses_titles_links_dates_and_the_publishers_name(self, monkeypatch) -> None:
        monkeypatch.setattr("joat.services.news._get", lambda url, accept: RSS)
        headlines = fetch_feed("https://bbc.example/rss", "technology", now=NOW)
        assert [h.title for h in headlines] == ["Apple raises prices", "Older story"]
        assert headlines[0].source == "BBC"
        assert headlines[0].category == "technology"
        assert headlines[0].guid == "bbc-1"
        assert headlines[0].published_at > headlines[1].published_at

    def test_google_news_names_the_publisher_per_entry(self, monkeypatch) -> None:
        monkeypatch.setattr("joat.services.news._get", lambda url, accept: GOOGLE)
        (headline,) = fetch_feed("https://news.google.com/rss", "top", now=NOW)
        assert headline.source == "Reuters"


class TestRows:
    def test_a_row_carries_age_title_and_source_within_the_list_cap(self) -> None:
        headline = Headline(
            title="A very " * 20 + "long headline",
            link="https://x", source="BBC", category="top",
            published_at=NOW - 180, guid="g",
        )
        row = headline.row(NOW)
        assert row.startswith("[3m] ")
        assert row.endswith(" · BBC")
        assert len(row) <= 64
        assert "…" in row


def service(tmp_path: Path, feeds: dict[str, list[Headline]], **kwargs) -> NewsService:
    def fetch(url: str, category: str) -> list[Headline]:
        if url not in feeds:
            raise OSError("down")
        return feeds[url]

    (tmp_path / "news.json").write_text(json.dumps({"categories": ["technology"]}))
    return NewsService(
        tmp_path / "news.json", fetch_feed=fetch,
        fetch_article=lambda url: ("Apple raises prices", "Full text. " * 120),
        clock=lambda: NOW, **kwargs,
    )


def headline(title: str, *, source: str = "BBC", age: float = 60.0, link: str | None = None) -> Headline:
    return Headline(
        title=title, link=link or f"https://x/{title}", source=source,
        category="technology", published_at=NOW - age, guid=title,
    )


class TestService:
    async def test_refresh_merges_feeds_newest_first_and_dedupes_the_same_story(self, tmp_path: Path) -> None:
        bbc, npr, google = CATEGORY_FEEDS["technology"]
        svc = service(tmp_path, {
            bbc: [headline("Apple raises prices", age=600)],
            npr: [headline("Rover lands on Mars", source="NPR", age=60)],
            google: [headline("Apple raises prices - Reuters", source="Reuters", age=30)],
        })
        assert await svc.refresh_once() == 2
        titles = [h.title for h in svc.headlines]
        assert titles == ["Apple raises prices - Reuters", "Rover lands on Mars"]
        rows = svc.snapshot().rows
        assert rows[0].startswith("[now] ") or rows[0].startswith("[0m] ")
        assert svc.fetched_at == NOW

    async def test_one_dead_feed_does_not_empty_the_tab(self, tmp_path: Path) -> None:
        bbc, _npr, _google = CATEGORY_FEEDS["technology"]
        svc = service(tmp_path, {bbc: [headline("Only story")]})
        assert await svc.refresh_once() == 1
        # A partial pass keeps the surviving feed's rows AND says so on the
        # first row (the audit's N1: nonempty snapshots hid the failure).
        assert svc.snapshot().rows == (
            "[!] Some feeds unavailable · partial list", "[1m] Only story · BBC",
        )
        assert svc.partial is True and svc.last_error is None
        # A tap counts that row; opening counts headlines.
        assert svc.status_rows == 1

    async def test_all_feeds_dead_keeps_yesterdays_headlines(self, tmp_path: Path) -> None:
        bbc, _npr, _google = CATEGORY_FEEDS["technology"]
        svc = service(tmp_path, {bbc: [headline("Kept")]})
        await svc.refresh_once()
        fetched_at = svc.fetched_at
        svc._fetch_feed = lambda url, category: (_ for _ in ()).throw(OSError())  # type: ignore[assignment]
        assert await svc.refresh_once() == 1
        assert [h.title for h in svc.headlines] == ["Kept"]
        # Kept, not refreshed: fetched_at does not move, the list opens with
        # its age, and a pass that changes nothing reports no change.
        assert svc.fetched_at == fetched_at and svc.last_error == "feeds unavailable"
        assert svc.snapshot().rows[0].startswith("[!] Feeds unavailable · showing headlines from ")
        assert await svc.refresh_changed() is False

    async def test_an_empty_tab_says_why(self, tmp_path: Path) -> None:
        svc = service(tmp_path, {})
        assert svc.snapshot().empty == "No headlines yet — long-press to refresh."
        await svc.refresh_once()
        assert svc.snapshot().empty == "Feeds unavailable — hold to retry."

    async def test_opening_a_headline_reads_the_article_into_pages(self, tmp_path: Path) -> None:
        bbc, _npr, _google = CATEGORY_FEEDS["technology"]
        svc = service(tmp_path, {bbc: [headline("Apple raises prices", age=120)]})
        await svc.refresh_once()
        reader = await svc.open(0)
        assert reader is not None
        assert reader.title == "NEWS · ARTICLE"
        assert reader.metadata == "BBC · 2m · Apple raises prices"
        assert len(reader.pages) > 1
        assert await svc.open(3) is None

    async def test_an_unreadable_article_still_opens_with_its_headline(self, tmp_path: Path) -> None:
        bbc, _npr, _google = CATEGORY_FEEDS["technology"]
        svc = service(tmp_path, {bbc: [headline("Paywalled")]})
        await svc.refresh_once()
        svc._fetch_article = lambda url: (_ for _ in ()).throw(OSError())  # type: ignore[assignment]
        reader = await svc.open(0)
        assert reader is not None
        assert "Paywalled" in reader.pages[0]

    @pytest.mark.parametrize("body", [
        "  First  paragraph\r\n\r\n" + "界" * 440 + "\r\n  LATER MARKER  ",
        "🙂" * 320 + "  LATER MARKER\n",
    ])
    async def test_article_reader_retains_exact_fetched_text(self, tmp_path: Path, body: str) -> None:
        bbc, _npr, _google = CATEGORY_FEEDS["technology"]
        svc = service(tmp_path, {bbc: [headline("Unicode article")]})
        svc._fetch_article = lambda url: ("Unicode article", body)  # type: ignore[assignment]
        await svc.refresh_once()
        reader = await svc.open(0)
        assert reader is not None
        assert "".join(reader.pages) == body
        assert len(reader.pages) > 1

    async def test_the_config_is_reread_when_the_mac_app_changes_it(self, tmp_path: Path) -> None:
        bbc_tech = CATEGORY_FEEDS["technology"][0]
        bbc_sport = CATEGORY_FEEDS["sports"][0]
        svc = service(tmp_path, {
            bbc_tech: [headline("Tech")], bbc_sport: [headline("Sport")],
        })
        await svc.refresh_once()
        assert [h.title for h in svc.headlines] == ["Tech"]
        path = tmp_path / "news.json"
        path.write_text(json.dumps({"categories": ["sports"]}))
        import os
        os.utime(path, (NOW + 10, NOW + 10))
        await svc.refresh_once()
        assert [h.title for h in svc.headlines] == ["Sport"]

    async def test_spoken_headlines_come_from_the_cache_numbered(self, tmp_path: Path) -> None:
        bbc, _npr, _google = CATEGORY_FEEDS["technology"]
        svc = service(tmp_path, {bbc: [headline("One"), headline("Two", age=120)]})
        assert svc.spoken_headlines() == "I don't have any headlines yet."
        await svc.refresh_once()
        assert svc.spoken_headlines() == "Here are the headlines. 1. One from BBC. 2. Two from BBC."


def test_summary_text_decodes_entities_and_breaks_related_coverage_rows() -> None:
    """A Google News summary is an <ol> of related coverage joined by
    &nbsp;&nbsp; — it must reach the glass as decoded rows, never as a
    run-on line with literal entities (observed on the simulator)."""
    from joat.services.news import _summary_text

    html = (
        '<ol><li><a href="x">Trump tells U.N. he could &#8216;annihilate&#8217; Iran</a>'
        "&nbsp;&nbsp;<font color=\"#6f6f6f\">NBC News</font></li>"
        '<li><a href="y">Live updates: Trump speaks at UNGA</a>&nbsp;&nbsp;<font>CNN</font></li></ol>'
    )
    assert _summary_text(html) == (
        "Trump tells U.N. he could ‘annihilate’ Iran NBC News\n"
        "Live updates: Trump speaks at UNGA CNN"
    )
    assert "&nbsp;" not in _summary_text(html)
    assert _summary_text("<p>One &amp; two</p>") == "One & two"
