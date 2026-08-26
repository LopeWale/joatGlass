"""Card formatting and delivery policy for HUD notifications.

The first pass shipped raw one-liners like

    !! [BatteriesAvocadoWidgetExtension:BatteriesAvocadoWidget] Your
    MacBook Pro and AirPods Pro are fully charged.

which is three failures at once: an internal bundle identifier where a
human name belongs, URGENT styling on a battery message, and no use of the
HUD's two-line shape. This module owns all three decisions so the channel
stays about plumbing.

Constraints that drive the design (docs/RULES.md): the HUD is 576x288
monochrome green — roughly 46 characters per line at the status font, and
a glance is one or two lines. Anything longer is not read while walking.
"""

from __future__ import annotations

import re
import time
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
    urgency: Urgency = Urgency.NOTIFY
    count: int = 1       # coalesced siblings
    at: float = field(default_factory=time.time)

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
            return Card(card.source, card.channel, card.summary,
                        card.urgency, count, now)
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
