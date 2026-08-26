"""Card formatting and delivery policy."""

from __future__ import annotations

from joat.channels.cards import (
    CardLog,
    Card,
    DeliveryPolicy,
    Urgency,
    humanize_app,
    is_systemish,
    shorten,
)


class TestHumanize:
    def test_bundle_ids_and_widget_classes_become_names(self) -> None:
        # Exactly the strings that reached the wearer's HUD unhumanized.
        assert humanize_app(
            "BatteriesAvocadoWidgetExtension:BatteriesAvocadoWidget") == "Battery"
        assert humanize_app("com.apple.MobileSMS") == "Messages"
        assert humanize_app("com.apple.news::com.apple.news.widget:today") == "News"
        assert humanize_app("com.apple.iCal") == "Calendar"

    def test_unknown_app_degrades_to_something_readable(self) -> None:
        assert humanize_app("") == "Alert"
        assert humanize_app("com.acme.FooBarWidgetExtension") == "Foo Bar"


class TestShorten:
    def test_breaks_on_word_boundary(self) -> None:
        out = shorten("the quick brown fox jumps over the lazy dog again", 20)
        assert out.endswith("…") and " " in out
        assert not out[:-1].endswith(" ")
        assert len(out) <= 21

    def test_short_text_untouched(self) -> None:
        assert shorten("hello") == "hello"


class TestRender:
    def test_person_card_leads_with_the_person(self) -> None:
        c = Card("Dele", "Messages", "are you coming?", Urgency.URGENT)
        lines = c.render().split("\n")
        assert lines[0] == "!! Dele · Messages"
        assert lines[1] == "are you coming?"

    def test_app_card_does_not_repeat_itself(self) -> None:
        c = Card("Battery", "Battery", "fully charged")
        assert c.render().split("\n")[0] == "Battery"   # not "Battery · Battery"

    def test_coalesced_count_is_shown(self) -> None:
        c = Card("Dele", "Messages", "hi", Urgency.NOTIFY, count=3)
        assert "(3)" in c.render().split("\n")[0]


class TestPolicy:
    def test_repeat_sender_coalesces_instead_of_stacking(self) -> None:
        p = DeliveryPolicy(coalesce_window_s=20)
        counts = [p.admit(Card("Dele", "Messages", f"m{i}"), now=1000 + i).count
                  for i in range(4)]
        assert counts == [1, 2, 3, 4]

    def test_rate_cap_drops_routine_but_lets_urgent_through(self) -> None:
        p = DeliveryPolicy(coalesce_window_s=0, max_per_minute=3)
        for i in range(3):
            assert p.admit(Card(f"s{i}", "App", "x"), now=1000) is not None
        assert p.admit(Card("s4", "App", "routine"), now=1000) is None
        assert p.admit(Card("s5", "App", "fire", Urgency.URGENT), now=1000) is not None

    def test_ignore_never_reaches_the_glasses(self) -> None:
        p = DeliveryPolicy()
        assert p.admit(Card("Promo", "App", "sale", Urgency.IGNORE)) is None


class TestSystemish:
    def test_recognizes_system_chatter(self) -> None:
        assert is_systemish("BatteriesAvocadoWidget", "Battery")
        assert is_systemish("com.apple.softwareupdate", "Update available")
        assert not is_systemish("com.apple.MobileSMS", "Dele")


class TestCardLog:
    """Cards were rendered and immediately forgotten — 'what did I miss?'
    was answered by a model with no record (audit finding 6)."""

    def test_remembers_newest_last(self) -> None:
        log = CardLog(keep=3)
        for c in ("a", "b", "c"):
            log.add(c, at=1.0)
        assert [c for _, c in log.recent(3)] == ["a", "b", "c"]

    def test_bounded_to_keep(self) -> None:
        log = CardLog(keep=2)
        for c in ("a", "b", "c", "d"):
            log.add(c, at=1.0)
        assert [c for _, c in log.recent(5)] == ["c", "d"]

    def test_empty_log_is_empty(self) -> None:
        assert CardLog().recent() == []
