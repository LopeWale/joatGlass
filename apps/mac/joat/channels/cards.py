"""Card formatting and delivery policy for HUD notifications.

The first pass shipped raw one-liners like

    !! [BatteriesAvocadoWidgetExtension:BatteriesAvocadoWidget] Your
    MacBook Pro and AirPods Pro are fully charged.

which is three failures at once: an internal bundle identifier where a
human name belongs, URGENT styling on a battery message, and no use of the
HUD's two-line shape. This module owns all three decisions so the channel
stays about plumbing.

Constraints that drive the design (docs/RULES.md): the HUD is 576x288
monochrome green and a glance is one or two lines — anything longer is not
read while walking. 46 chars per card line is measured, not folklore: cards
render through the assistant overlay's 386px body (HudTypography TYPE.body,
14px proportional, ~6.6px mean advance → ~58 chars), so 46 keeps a full
line visible with margin even in the overlay's narrowest fit.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum

HUD_LINE_CHARS = 46


class Urgency(IntEnum):
    IGNORE = 0
    NOTIFY = 1
    URGENT = 2


# Identifiers that are never a person and never urgent, however the model
# feels about them. A battery reaching 100% is not an interruption.
_SYSTEM_HINTS = (
    "battery", "batteries", "widget", "screentime", "softwareupdate",
    "timemachine", "airport", "bluetooth", "wifi", "icloud", "storage",
)

_APP_NAMES = {
    "mobilesms": "Messages", "messages": "Messages", "mail": "Mail",
    "ical": "Calendar", "calendar": "Calendar", "reminders": "Reminders",
    "facetime": "FaceTime", "photos": "Photos", "music": "Music",
    "news": "News", "weather": "Weather", "batteries": "Battery",
    "screentime": "Screen Time", "slack": "Slack", "discord": "Discord",
}


def humanize_app(raw: str) -> str:
    """'BatteriesAvocadoWidgetExtension:BatteriesAvocadoWidget' -> 'Battery'.

    Notification sources hand us bundle ids, widget-extension class names,
    and occasionally a real display name. Reduce all three to something a
    person recognizes at a glance.
    """
    if not raw:
        return "Alert"
    # Scan the WHOLE identifier first: splitting "com.apple.MobileSMS" into
    # CamelCase words yields "Mobile"/"SMS", neither of which is a known
    # name, and "com.apple.news::...widget:today" would reduce to "today".
    lowered = raw.lower()
    for key in sorted(_APP_NAMES, key=len, reverse=True):
        if key in lowered:
            return _APP_NAMES[key]
    token = raw.split(":")[-1].rsplit(".", 1)[-1]
    # Split CamelCase so "BatteriesAvocadoWidget" -> [Batteries, Avocado, ...]
    words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", token) or [token]
    for word in words:
        name = _APP_NAMES.get(word.lower())
        if name is not None:
            return name
    # Drop implementation noise, keep the meaningful head.
    words = [w for w in words
             if w.lower() not in ("widget", "extension", "avocado", "app", "ui")]
    return " ".join(words[:2]) or "Alert"


def is_systemish(app: str, title: str) -> bool:
    """System chatter: deliverable, but never allowed to claim urgency."""
    blob = f"{app} {title}".lower()
    return any(hint in blob for hint in _SYSTEM_HINTS)


def shorten(text: str, limit: int = HUD_LINE_CHARS) -> str:
    """Trim on a word boundary — a mid-word cut reads as a glitch."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return (cut or text[:limit]).rstrip(",.;:") + "…"


@dataclass
class Card:
    """One HUD-ready notification."""

    source: str          # who/what: "Dele", "Battery"
    channel: str         # where from: "Messages", "Calendar"
    summary: str
    # The FULL notification text, untruncated. Glance surfaces (tray row,
    # overlay card) draw from `summary` through their measured budgets; the
    # NOTIFICATIONS · DETAIL page reads THIS, because "the HUD is glanced
    # at" was never a licence to cut the wearer's message to 46 chars on a
    # ten-row page (wearer's report, 2026-09-05: "Our message is cutting
    # off. Why? Can I not see full messages?"). Empty means summary is all
    # the source had.
    body: str = ""
    urgency: Urgency = Urgency.NOTIFY
    count: int = 1       # coalesced siblings
    at: float = field(default_factory=time.time)
    # Machine taxonomy ("messages.incoming_text", "reminders.due"), so a
    # reader can route on the kind of thing that happened rather than
    # re-parsing rendered text. Matches the vocabulary the observation store
    # already uses.
    event_class: str = ""
    # The producing store's own id, for dedup across sources.
    origin_id: str | None = None
    # JOAT presentation identity; never a native Messages/Notification identifier.
    card_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def home_row(self, now: float | None = None) -> str:
        """One home-screen notification row: "Source · age · headline".

        The wearer reads their notifications on the resting screen. A count
        would only tell them to open a page; the reference HUDs show the
        cards themselves, so JOAT does too.
        """
        elapsed = max(0.0, (now if now is not None else time.time()) - self.at)
        if elapsed < 60:
            age = "now"
        elif elapsed < 3_600:
            age = f"{int(elapsed // 60)}m"
        elif elapsed < 86_400:
            age = f"{int(elapsed // 3_600)}h"
        else:
            age = f"{int(elapsed // 86_400)}d"
        headline = " ".join(self.summary.split())[:96]
        return f"{self.source} · {age} · {headline}"

    def render(self) -> str:
        """Two lines: who, then what. The HUD is glanced at, not read."""
        mark = "!! " if self.urgency is Urgency.URGENT else ""
        who = self.source or self.channel
        header = f"{mark}{who}"
        if self.channel and self.channel.lower() != who.lower():
            header += f" · {self.channel}"
        if self.count > 1:
            header += f" ({self.count})"
        return f"{shorten(header)}\n{shorten(self.summary)}"


class DeliveryPolicy:
    """Decides what actually reaches the glasses, and when.

    Two protections triage alone cannot give the wearer: coalescing (five
    messages from one person are one card, not five) and a rate cap (a sync
    storm must not turn the HUD into a slot machine).
    """

    def __init__(self, coalesce_window_s: float = 20.0,
                 max_per_minute: int = 6) -> None:
        self._window = coalesce_window_s
        self._max_per_minute = max_per_minute
        self._recent: list[float] = []
        self._last_by_source: dict[str, tuple[float, int]] = {}

    def admit(self, card: Card, now: float | None = None) -> Card | None:
        """Return the card to show (possibly coalesced), or None to drop."""
        now = time.time() if now is None else now
        if card.urgency is Urgency.IGNORE:
            return None

        key = f"{card.channel}{card.source}"
        prev = self._last_by_source.get(key)
        if prev is not None and now - prev[0] < self._window:
            count = prev[1] + 1
            self._last_by_source[key] = (now, count)
            # Same person again inside the window: replace, don't stack.
            # Carry the taxonomy fields explicitly — a coalesced repeat is
            # still the same kind of event, and positional construction
            # would silently drop them.
            return Card(card.source, card.channel, card.summary,
                        body=card.body, urgency=card.urgency, count=count, at=now,
                        event_class=card.event_class, origin_id=card.origin_id)
        self._last_by_source[key] = (now, 1)

        self._recent = [t for t in self._recent if now - t < 60.0]
        if len(self._recent) >= self._max_per_minute:
            # Over budget: only genuine urgency still gets through.
            if card.urgency is not Urgency.URGENT:
                return None
        self._recent.append(now)
        return card


# Words that make a message actually time-sensitive. The local 3B model is
# not reliable enough to be trusted alone on urgency — asked to triage, it
# echoed the prompt's own definition back ("Person waiting on reply,
# deadline firing") and marked routine texts URGENT. Model judgement is
# therefore ADVISORY: it can propose URGENT, but the text must corroborate.
_URGENT_SIGNALS = (
    "urgent", "asap", "emergency", "right now", "immediately", "call me",
    "help", "911", "hospital", "stuck", "locked out", "waiting for you",
    "where are you", "you there", "on my way", "running late", "deadline",
)

# Prompt echo: the model repeating instruction text instead of summarizing.
_ECHO_MARKERS = (
    "person waiting", "deadline firing", "worth a glance", "needs attention",
    "promotional, automated", "smart-glasses", "level is",
)


def looks_like_echo(summary: str) -> bool:
    low = summary.lower()
    return any(marker in low for marker in _ECHO_MARKERS)


def corroborates_urgency(text: str) -> bool:
    low = text.lower()
    return any(signal in low for signal in _URGENT_SIGNALS)


class CardLog:
    """The last cards shown, so "what did I miss?" has a real source.

    Cards were rendered to the HUD and immediately forgotten — the model was
    asked about notifications it had no record of. Bounded; newest last.

    Entries are whole `Card`s, not pre-rendered text: a reader that only
    kept the rendered string had thrown away urgency, channel, and kind, so
    the Notifications view could do nothing but replay old HUD lines.
    """

    def __init__(self, keep: int = 10) -> None:
        self._keep = keep
        self._entries: list[Card] = []
        self._acknowledged: set[str] = set()
        self._delivering: set[str] = set()
        self._early_acknowledged: set[str] = set()
        self._on_change: Callable[[], None] | None = None

    def add(self, card: Card) -> None:
        self._entries.append(card)
        del self._entries[:-self._keep]
        self._acknowledged.intersection_update(card.card_id for card in self._entries)
        self._changed()

    def recent(self, limit: int = 5) -> list[Card]:
        return self._entries[-max(1, min(limit, self._keep)):]

    def on_change(self, callback: Callable[[], None] | None) -> None:
        """Notify the composition root when JOAT's active notification set changes."""
        self._on_change = callback

    def _changed(self) -> None:
        if self._on_change is not None:
            self._on_change()

    def active(self) -> list[Card]:
        """The same bounded pending cards used by the tray and its bell."""
        return [card for card in self._entries if card.card_id not in self._acknowledged]

    def active_count(self) -> int:
        return len(self.active())

    def acknowledge(self, card_id: str) -> bool:
        """Read/dismiss only this JOAT card, retaining history for AI recall."""
        if card_id in self._delivering:
            self._early_acknowledged.add(card_id)
            return True
        if card_id in self._acknowledged or not any(c.card_id == card_id for c in self._entries):
            return False
        self._acknowledged.add(card_id)
        self._changed()
        return True

    def begin_delivery(self, card_id: str) -> None:
        """Permit an early tap without claiming an unsent card as delivered."""
        self._delivering.add(card_id)

    def finish_delivery(self, card: Card, *, accepted: bool) -> None:
        self._delivering.discard(card.card_id)
        acknowledged = card.card_id in self._early_acknowledged
        self._early_acknowledged.discard(card.card_id)
        if accepted:
            if acknowledged:
                self._acknowledged.add(card.card_id)
            self.add(card)

    def dismiss_snapshot(self, card_ids: list[str]) -> None:
        """Clear the displayed snapshot, never arrivals received after it was shown."""
        retained = {card.card_id for card in self._entries}
        before = len(self._acknowledged)
        self._acknowledged.update(retained.intersection(card_ids))
        if len(self._acknowledged) != before:
            self._changed()
