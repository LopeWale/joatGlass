"""Which timezone the wearer is standing in.

The Mac is not the answer. Every time string JOAT shows or says was being
formatted from ``/etc/localtime`` — not because anyone chose the Mac's zone
but because that is what Python hands you when nothing says otherwise. The
HUD's own clock, meanwhile, is ``new Date()`` in the phone's WebView. Two
defaults on two machines, never reconciled: fly to London and the banner
reads 22:41 while "Design review · 4:30" still reads Chicago, and the one
thing a wearer wants from that pair — how long until — cannot be computed.

So the phone tells us. It sends an IANA name it gets for free from
``Intl.DateTimeFormat().resolvedOptions().timeZone`` (the Even Hub SDK
exposes no timezone of its own; ``UserInfo`` carries ``country`` and
nothing more), on ``hello`` and again with every position fix, so landing
somewhere new corrects the HUD without waiting for a reconnect.

Two zones, not one, because two questions are being asked:

* ``current()`` — where the wearer is. Timed events convert into it: an
  instant is an instant, and it should be rendered where it will be read.
* ``home()`` — where the Mac is, which is the zone the calendar's all-day
  events were authored in. An all-day event is a DATE, not an instant.
  Converting it moves it across midnight and silently loses or gains a
  day, so day-bucketing for all-day entries must stay home.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone, tzinfo
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

#: Longest IANA name worth entertaining. The real maximum is well under
#: this ("America/Argentina/ComodRivadavia" is 33); the cap exists so a
#: malformed frame cannot push an unbounded string into ZoneInfo's lookup.
MAX_ZONE_NAME = 64


def _default_home() -> tzinfo:
    """The Mac's own zone, as a real ZoneInfo where the OS exposes one.

    A fixed UTC offset from ``datetime.now().astimezone()`` looks usable
    but shifts future dates into the wrong civil day at a DST transition.
    """
    return datetime.now().astimezone().tzinfo or timezone.utc


class WearerZone:
    """Where the wearer is, with the Mac's zone as the fallback.

    Falling back rather than failing is deliberate: the engine runs before
    any plugin connects, and every consumer here formats something a human
    reads. The Mac's zone is a defensible guess; a crash is not.
    """

    def __init__(self, home: Callable[[], tzinfo] = _default_home) -> None:
        self._home = home
        self._name: str | None = None
        self._zone: tzinfo | None = None

    def accept(self, name: str | None) -> bool:
        """Take an IANA name from the phone. True when it changed the zone.

        Unknown or malformed names are refused and logged rather than
        raising: a host that reports a zone this build's tzdata has never
        heard of should degrade to the Mac's zone, not take the link down.
        """
        if name is None or not isinstance(name, str):
            return False
        candidate = name.strip()
        if not candidate or len(candidate) > MAX_ZONE_NAME:
            return False
        if candidate == self._name:
            return False
        try:
            zone = ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            log.warning("phone reported an unknown timezone %r — keeping %s", candidate, self.name())
            return False
        self._name = candidate
        self._zone = zone
        log.info("wearer timezone is now %s (from the phone)", candidate)
        return True

    def current(self) -> tzinfo:
        """Where the wearer is: the phone's zone, else the Mac's."""
        return self._zone if self._zone is not None else self._home()

    def home(self) -> tzinfo:
        """Where the Mac is — the zone all-day events were authored in."""
        return self._home()

    def name(self) -> str | None:
        """The phone's IANA name, or None while the Mac's zone stands in."""
        return self._name

    def travelling(self) -> bool:
        """True when the wearer's zone differs from the Mac's right now.

        Compared by current OFFSET, not by name: two names can agree today
        and diverge at a DST boundary, and what matters to a reader is
        whether the clock they see differs from the one the Mac assumes.
        """
        if self._zone is None:
            return False
        now = datetime.now(timezone.utc)
        return now.astimezone(self._zone).utcoffset() != now.astimezone(self._home()).utcoffset()
