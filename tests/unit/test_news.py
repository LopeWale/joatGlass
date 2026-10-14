"""The News tab: free feeds, fetched on the Mac, read on the glass."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from joat.services.news import (
    CATALOG,
    DEFAULT_CATEGORIES,
    Headline,
    NewsConfig,
    NewsService,
    fetch_feed,
)

NOW = 1_800_000_000.0
#: The technology and sports defaults (news_sources.json), in catalog order.
TECH = tuple(s.url for s in CATALOG["technology"] if s.default)
SPORT = tuple(s.url for s in CATALOG["sports"] if s.default)
#: Rows name a catalog feed's publisher by its catalog name.
TECH_NAME = next(s.name for s in CATALOG["technology"] if s.default)

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
        defaults = [s.url for s in CATALOG["technology"] if s.default]
        assert feeds[: len(defaults)] == tuple(("technology", url) for url in defaults)
        assert feeds[-1] == ("custom", "https://example.org/feed.xml")

    def test_custom_feeds_alone_are_a_complete_configuration(self) -> None:
        config = NewsConfig.from_dict({"categories": [], "customFeeds": ["https://a/b"]})
        assert config.categories == ()
        assert config.feeds() == (("custom", "https://a/b"),)
        assert config.to_dict()["customFeeds"] == ["https://a/b"]


class TestCatalog:
    """news_sources.json: the free sources, chosen per category."""

    def test_every_category_has_defaults_and_every_source_a_unique_id(self) -> None:
        ids = [s.id for sources in CATALOG.values() for s in sources]
        assert len(ids) == len(set(ids))
        for category, sources in CATALOG.items():
            assert any(s.default for s in sources), category
            for source in sources:
                assert source.url.startswith("https://"), source.id
                assert "news.google.com" not in source.url
        assert set(DEFAULT_CATEGORIES) <= set(CATALOG)

    def test_an_unchanged_choice_reads_each_categorys_defaults(self) -> None:
        config = NewsConfig.from_dict({"categories": ["science"]})
        assert config.sources is None
        assert [s.id for s in config.enabled_sources()] == [
            s.id for s in CATALOG["science"] if s.default
        ]
        assert "sources" not in config.to_dict()

    def test_a_chosen_set_is_read_exactly_and_unknown_ids_are_dropped(self) -> None:
        config = NewsConfig.from_dict({
            "categories": ["science", "world"],
            "sources": ["npr-science", "dw", "made-up"],
        })
        assert [s.id for s in config.enabled_sources()] == ["npr-science", "dw"]
        assert config.to_dict()["sources"] == ["npr-science", "dw"]
        # A chosen source in a category that is switched off is not polled.
        off = NewsConfig.from_dict({"categories": ["science"], "sources": ["npr-science", "dw"]})
        assert [s.id for s in off.enabled_sources()] == ["npr-science"]

    def test_a_subject_with_none_of_its_sources_chosen_reads_its_defaults(self) -> None:
        # Sports switched on after the wearer picked World's sources.
        config = NewsConfig.from_dict({"categories": ["world", "sports"], "sources": ["dw"]})
        assert [s.id for s in config.enabled_sources()] == [
            "dw", *(s.id for s in CATALOG["sports"] if s.default)
        ]

    async def test_rows_name_the_catalog_publisher_not_the_feed_title(self, tmp_path: Path) -> None:
        path = tmp_path / "news.json"
        path.write_text(json.dumps({"categories": ["world"], "sources": ["cbs-world"]}))

        def fake_feed(url: str, category: str) -> list[Headline]:
            # CBS titles this feed "World"; the row must say "CBS News".
            return [Headline(title="A story", link="https://cbs.example/1", source="World",
                             category=category, published_at=NOW, guid="1")]

        service = NewsService(path, fetch_feed=fake_feed, clock=lambda: NOW)
        await service.refresh_once()
        assert [h.source for h in service.headlines] == ["CBS News"]


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

    def test_sportsbook_promotions_are_not_headlines(self, monkeypatch) -> None:
        promo = RSS.replace(b"<title>Older story</title>",
                            b"<title>Use this promo code to get $350 in bonus bets tonight</title>")
        monkeypatch.setattr("joat.services.news._get", lambda url, accept: promo)
        assert [h.title for h in fetch_feed("https://x.example/rss", "sports", now=NOW)] == ["Apple raises prices"]

    def test_shopping_guides_are_not_headlines(self, monkeypatch) -> None:
        shop = RSS.replace(b"https://bbc.example/2", b"https://news.example/select/shopping/best-advent-calendars")
        monkeypatch.setattr("joat.services.news._get", lambda url, accept: shop)
        assert [h.title for h in fetch_feed("https://x.example/rss", "top", now=NOW)] == ["Apple raises prices"]


async def test_the_feeds_wording_of_the_headline_is_not_the_first_paragraph(tmp_path: Path) -> None:
    feed_title = "Musk says any limits on access are down to the US"

    def fake_feed(url: str, category: str) -> list[Headline]:
        return [Headline(title=feed_title, link="https://bbc.example/9", source="BBC",
                         category=category, published_at=NOW, guid="9")]

    service = NewsService(
        tmp_path / "news.json", fetch_feed=fake_feed, clock=lambda: NOW,
        fetch_article=lambda url: ("Starlink: Musk points at the US government", f"{feed_title}\n\nThe story."),
    )
    await service.refresh_once()
    reader = await service.open(0)
    assert reader is not None
    assert reader.headline == "Starlink: Musk points at the US government"
    assert "".join(reader.pages).strip() == "The story."


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
        bbc, npr, google = TECH
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
        bbc, _npr, _google = TECH
        svc = service(tmp_path, {bbc: [headline("Only story")]})
        assert await svc.refresh_once() == 1
        # A partial pass keeps the surviving feed's rows AND says so on the
        # first row (the audit's N1: nonempty snapshots hid the failure).
        assert svc.snapshot().rows == (
            "[!] Some feeds unavailable · partial list", f"[1m] Only story · {TECH_NAME}",
        )
        assert svc.partial is True and svc.last_error is None
        # A tap counts that row; opening counts headlines.
        assert svc.status_rows == 1

    async def test_a_dead_feed_is_logged_by_host_and_status(self, tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
        # "category=custom (HTTPError)" was logged ~1,900 times without
        # saying which of four custom feeds was dead.
        import urllib.error

        bbc, _npr, _google = TECH

        def fetch(url: str, category: str) -> list[Headline]:
            if url == bbc:
                return [headline("Only story")]
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

        svc = service(tmp_path, {})
        svc._fetch_feed = fetch  # type: ignore[assignment]
        with caplog.at_level("INFO", logger="joat.services.news"):
            await svc.refresh_once()
        lines = [record.getMessage() for record in caplog.records if "feed unavailable" in record.getMessage()]
        assert lines and all("HTTPError 404" in line for line in lines)
        assert all("host=" in line and "host=?" not in line for line in lines)
        # The host only: never the feed's path or query.
        assert all("/" not in line.split("host=", 1)[1].split(" ", 1)[0] for line in lines)

    async def test_all_feeds_dead_keeps_yesterdays_headlines(self, tmp_path: Path) -> None:
        bbc, _npr, _google = TECH
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
        bbc, _npr, _google = TECH
        svc = service(tmp_path, {bbc: [headline("Apple raises prices", age=120)]})
        await svc.refresh_once()
        reader = await svc.open(0)
        assert reader is not None
        assert reader.title == "NEWS · ARTICLE"
        # The article template: headline, then "Source · age", then text.
        assert reader.headline == "Apple raises prices"
        assert reader.metadata == f"{TECH_NAME} · 2m"
        assert len(reader.pages) > 1
        assert await svc.open(3) is None

    async def test_an_unreadable_article_still_opens_with_its_headline(self, tmp_path: Path) -> None:
        bbc, _npr, _google = TECH
        svc = service(tmp_path, {bbc: [headline("Paywalled")]})
        await svc.refresh_once()
        svc._fetch_article = lambda url: (_ for _ in ()).throw(OSError())  # type: ignore[assignment]
        reader = await svc.open(0)
        assert reader is not None
        assert reader.headline == "Paywalled"
        assert reader.pages[0].startswith("The article text could not be read")

    @pytest.mark.parametrize("body", [
        "  First  paragraph\r\n\r\n" + "界" * 440 + "\r\n  LATER MARKER  ",
        "🙂" * 320 + "  LATER MARKER\n",
    ])
    async def test_article_reader_retains_exact_fetched_text(self, tmp_path: Path, body: str) -> None:
        bbc, _npr, _google = TECH
        svc = service(tmp_path, {bbc: [headline("Unicode article")]})
        svc._fetch_article = lambda url: ("Unicode article", body)  # type: ignore[assignment]
        await svc.refresh_once()
        reader = await svc.open(0)
        assert reader is not None
        assert "".join(reader.pages) == body
        assert len(reader.pages) > 1

    async def test_the_config_is_reread_when_the_mac_app_changes_it(self, tmp_path: Path) -> None:
        bbc_tech = TECH[0]
        bbc_sport = SPORT[0]
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
        bbc, _npr, _google = TECH
        svc = service(tmp_path, {bbc: [headline("One"), headline("Two", age=120)]})
        assert svc.spoken_headlines() == "I don't have any headlines yet."
        await svc.refresh_once()
        assert svc.spoken_headlines() == f"Here are the headlines. 1. One from {TECH_NAME}. 2. Two from {TECH_NAME}."


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


class TestTidyArticle:
    """Every publisher's page in one shape (news.tidy_article). The inputs
    are written for the test, after the patterns real pages produced on
    2026-10-14 (BBC, Ars Technica, Phys.org, ScienceDaily, DW, CBS, Live
    Science, Al Jazeera)."""

    def test_paragraphs_are_spaced_alike_and_furniture_is_dropped(self) -> None:
        from joat.services.news import tidy_article

        raw = "\n".join([
            "- Published",
            "Recommended Stories",
            "list of 4 items",
            "The council voted on Tuesday to widen the bridge.",
            "Loading...",
            "Work starts in the spring.",
            "Watch on Example",
            "Popular on Example",
            "CBS/AP",
            ".",
            "- Published28 August",
            "Related topics",
        ])
        assert tidy_article(raw, title="Bridge to be widened") == (
            "The council voted on Tuesday to widen the bridge.\n\nWork starts in the spring."
        )

    def test_captions_with_their_credits_and_repeats_go(self) -> None:
        from joat.services.news import tidy_article

        raw = "\n".join([
            "A probe is lowered into a test furnace.",
            "Credit:",
            "Agency/Photographer",
            "A probe is lowered into a test furnace.",
            "Credit:",
            "Agency/Photographer",
            "The mission is back on schedule.",
        ])
        assert tidy_article(raw) == "The mission is back on schedule."

    def test_the_headline_its_restatement_dates_and_editor_credits_go(self) -> None:
        from joat.services.news import tidy_article

        raw = "\n".join([
            "Sam Writer",
            "Scientific Editor",
            "October 14, 2026",
            "City says bridge will be widened next spring",
            "City to widen bridge next spring",
            "Officials announced the plan on Tuesday.",
        ])
        assert tidy_article(raw, title="City to widen bridge next spring") == (
            "Officials announced the plan on Tuesday."
        )

    def test_labelled_metadata_end_matter_and_the_author_bio_go(self) -> None:
        from joat.services.news import tidy_article

        raw = "\n".join([
            "- Date:",
            "- October 14, 2026",
            "- Source:",
            "- Example University",
            "- Summary:",
            "- Cats with long legs carried a rare gene.",
            "- Share:",
            "Researchers studied two cats.",
            "The gene was confirmed in both.",
            "Pat is a senior science reporter at Example News.",
            "Story Source:",
            "Materials provided by Example University.",
            "Cite This Page:",
        ])
        # "Share:" is a bare label: the story's first paragraph follows it.
        assert tidy_article(raw) == (
            "Cats with long legs carried a rare gene.\n\n"
            "Researchers studied two cats.\n\n"
            "The gene was confirmed in both."
        )

    def test_credits_reader_callouts_and_transcript_notices_close_nothing(self) -> None:
        from joat.services.news import tidy_article

        raw = "\n".join([
            "The trial opened on Wednesday.",
            "The defense asked for a dismissal.",
            "Court dates run through February.",
            "With additional reporting by Sam Writer in Hamburg",
            "Pat Producer produced and edited this interview for broadcast. Lee Web adapted it for the web.",
            "For more stories like this, subscribe to the Example World newsletter.",
            "Get in touch",
            "Have you been affected by issues covered in this story? Share your experiences.",
        ])
        assert tidy_article(raw) == (
            "The trial opened on Wednesday.\n\n"
            "The defense asked for a dismissal.\n\n"
            "Court dates run through February."
        )

    def test_a_reference_ends_the_story_and_takes_the_bio_with_it(self) -> None:
        from joat.services.news import tidy_article

        raw = "\n".join([
            "Pain lit up an unexpected network.",
            "The team scanned 40 volunteers.",
            "This article is for informational purposes only and is not meant to offer medical advice.",
            "Doe, J., & Roe, R. (2026). Brain networks in back pain. Example Journal. https://doi.org/10.0000/x.1",
            "Jo Doe earned her PhD in genetics and writes for several publications.",
        ])
        assert tidy_article(raw) == "Pain lit up an unexpected network.\n\nThe team scanned 40 volunteers."

    def test_a_teaser_card_keeps_its_summary_once(self) -> None:
        from joat.services.news import tidy_article

        title = "A rocky first briefing for a new press secretary"
        raw = "\n".join([
            f"{title}The new press secretary left many questions unanswered.",
            "The new press secretary left many questions unanswered.",
            "NPR does not offer or accept money for coverage or interviews.",
            "Anyone who makes such an offer or request is not an NPR employee.",
        ])
        assert tidy_article(raw, title=title) == "The new press secretary left many questions unanswered."

    def test_a_republish_box_ends_the_story_however_early(self) -> None:
        from joat.services.news import tidy_article

        raw = "\n".join([
            "Reporters look at the week's three biggest stories.",
            "Catch these stories and more on the latest episode.",
            '<p class="small">Reporters look at the week\'s three biggest stories.</p>',
            '<p>This <a href="https://example.org/x">article</a> first appeared on Example Watch.</p>',
            "Copy to Clipboard",
            "First Watch Newsletter",
            "Sign up",
            "Sign up today for First Watch!",
            "(SOUNDBITE OF MUSIC)",
            "A closing line that would have counted, had the box not come first.",
            "One more.",
            "And another.",
            "And the last.",
        ])
        assert tidy_article(raw) == (
            "Reporters look at the week's three biggest stories.\n\n"
            "Catch these stories and more on the latest episode."
        )

    def test_the_title_loses_only_the_publishers_own_name(self) -> None:
        from joat.services.news import tidy_title

        assert tidy_title("States sue a chatbot maker - Engadget", "Engadget") == "States sue a chatbot maker"
        assert tidy_title("Care managers help families - KFF Health News", "KFF Health News") == (
            "Care managers help families"
        )
        # A dash that is part of the headline stays.
        assert tidy_title("Rates hold - for now", "Example") == "Rates hold - for now"
