"""Notification Center → triage → HUD cards (ROADMAP Phase 1).

Reads macOS's notification store directly (the `usernoted` sqlite db),
triages each new notification with the local MLX model, and delivers
cards through the SessionManager's interrupt policy. Metric this serves:
triaged alert on the HUD < 5 s from delivery.

Reading the db requires Full Disk Access for the hosting app — without
it the channel logs one warning and stays dormant (the brain runs fine).
The db is opened immutable+read-only so we can never corrupt or lock the
system's store.

Schema notes (classic db2 layout, introspected defensively at runtime):
`record(rec_id, app_id, delivered_date, data, ...)` where `data` is a
binary plist with req={titl, subt, body}; `app(app_id, identifier)` maps
to bundle ids. Dates are Cocoa epoch (seconds since 2001-01-01).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import plistlib
import sqlite3
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from dataclasses import dataclass
from pathlib import Path

from ..core.interfaces import ReplyEngine
from .event_taxonomy import classify_event_class
from .cards import (
    Card,
    DeliveryPolicy,
    Urgency,
    corroborates_urgency,
    humanize_app,
    is_systemish,
    looks_like_echo,
)

log = logging.getLogger(__name__)

COCOA_EPOCH_UNIX = 978_307_200  # 2001-01-01T00:00:00Z

DEFAULT_DB = Path(
    "~/Library/Group Containers/group.com.apple.usernoted/db2/db"
).expanduser()


@dataclass(frozen=True, slots=True)
class Notification:
    rec_id: int
    app: str
    title: str
    subtitle: str
    body: str
    delivered_unix: float
    native_id: str | None = None


def _parse_record(rec_id: int, app: str, delivered: float | None, blob: bytes) -> Notification | None:
    try:
        data = plistlib.loads(blob)
    except Exception:
        return None
    req = data.get("req") or {}
    title = str(req.get("titl") or "")
    subtitle = str(req.get("subt") or "")
    body = str(req.get("body") or "")
    if not (title or subtitle or body):
        return None
    when = (delivered or 0.0) + COCOA_EPOCH_UNIX if delivered else 0.0
    return Notification(rec_id, app, title, subtitle, body, when, str(rec_id))


class NotificationStore:
    """Read-only view of the usernoted db. Injectable path for tests."""

    def __init__(self, db_path: Path = DEFAULT_DB) -> None:
        self._path = db_path

    @property
    def available(self) -> bool:
        return self._path.is_file() and self._path.stat().st_size > 0

    def latest_rec_id(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COALESCE(MAX(rec_id), 0) FROM record").fetchone()
            return int(row[0])

    def read_since(self, rec_id: int) -> list[Notification]:
        with self._connect() as conn:
            has_app = bool(conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='app'"
            ).fetchone())
            if has_app:
                rows = conn.execute(
                    "SELECT r.rec_id, COALESCE(a.identifier, ''), r.delivered_date, r.data "
                    "FROM record r LEFT JOIN app a ON r.app_id = a.app_id "
                    "WHERE r.rec_id > ? ORDER BY r.rec_id", (rec_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT rec_id, '', delivered_date, data FROM record "
                    "WHERE rec_id > ? ORDER BY rec_id", (rec_id,),
                ).fetchall()
        out = []
        for rid, app, delivered, blob in rows:
            n = _parse_record(int(rid), str(app), delivered, blob)
            if n is not None:
                out.append(n)
        return out

    def _connect(self) -> sqlite3.Connection:
        # A missing store must never be CREATED by a reader — connecting to a
        # nonexistent path can leave a stub file in Apple's container.
        if not self._path.is_file() or self._path.stat().st_size == 0:
            raise FileNotFoundError(f"no notification store at {self._path}")
        return sqlite3.connect(f"file:{self._path}?immutable=1", uri=True)

    def baseline(self) -> int:
        return self.latest_rec_id()

    def read_new(self, watermark: Any) -> tuple[list[Notification], Any]:
        fresh = self.read_since(watermark)
        newest = max([watermark, *(n.rec_id for n in fresh)])
        return fresh, newest


TRIAGE_PROMPT = (
    "Triage one notification for smart-glasses. Answer with EXACTLY one "
    "line: LEVEL — summary.\n"
    "LEVEL is URGENT, NOTIFY, or IGNORE.\n"
    "URGENT is RARE: a person is waiting on a reply right now, a deadline "
    "or alarm is firing, or something is wrong. Almost nothing else.\n"
    "NOTIFY: worth a glance when convenient (most messages, most alerts).\n"
    "IGNORE: promotional, automated, or noise.\n"
    "Never mark a battery, sync, update, or widget notification URGENT.\n"
    "The summary is under 10 words, plain, no app name, no quotes.\n\n"
    "App: {app}\nArrived: {age}\nTitle: {title}\nSubtitle: {subtitle}\nBody: {body}"
)


def humanize_age(seconds: float) -> str:
    """'12 seconds ago' / '40 minutes ago' — recency is the strongest
    predictor of 'someone is waiting on you right now', and the triager was
    deciding urgency without it."""
    if seconds < 60:
        return f"{max(0, int(seconds))} seconds ago"
    if seconds < 3600:
        return f"{int(seconds / 60)} minutes ago"
    return f"{seconds / 3600:.1f} hours ago"


def parse_triage(reply: str) -> tuple[Urgency, str] | None:
    head = reply.strip().splitlines()[0] if reply.strip() else ""
    for name, level in (("URGENT", Urgency.URGENT), ("NOTIFY", Urgency.NOTIFY),
                        ("IGNORE", Urgency.IGNORE)):
        if head.upper().startswith(name):
            summary = head[len(name):].lstrip(" —-–:").strip()
            return level, summary
    return None


# Channels where the notification TITLE is a person, not an app. The same
# set decides the card's `source`, so both derive from one place.
PERSON_CHANNELS = ("Messages", "Mail", "Slack", "Discord")


class NotificationSource(Protocol):
    """Watermark-generic store: sqlite (legacy) or Biome (macOS 26+)."""

    def baseline(self) -> Any: ...
    def read_new(self, watermark: Any) -> tuple[list[Notification], Any]: ...


class NotificationChannel:
    """Poll → triage → deliver. deliver() is the SessionManager seam."""

    def __init__(
        self,
        store: NotificationSource,
        triage: ReplyEngine | None,
        deliver: Callable[[Card], None | Awaitable[None]],
        poll_interval_s: float = 2.0,
        status: object | None = None,
        policy: DeliveryPolicy | None = None,
        triage_timeout_s: float = 5.0,
        enrich_body: Callable[[str, str, float], str | None] | None = None,
    ) -> None:
        if triage_timeout_s <= 0:
            raise ValueError("triage timeout must be positive")
        self._store = store
        # (channel, preview, delivered_unix) -> full text, or None. Supplied
        # by the Mac for channels whose authoritative store it can read.
        self._enrich_body = enrich_body
        self._triage = triage
        self._deliver = deliver
        self._interval = poll_interval_s
        self._status = status
        self._policy = policy or DeliveryPolicy()
        self._triage_timeout_s = triage_timeout_s
        self._running = False

    async def run(self) -> None:
        try:
            last = await asyncio.to_thread(self._store.baseline)
        except Exception as exc:
            log.warning(
                "notification channel dormant — cannot read the store (%s). "
                "Grant Full Disk Access to the app hosting the brain and restart.",
                exc,
            )
            return
        self._running = True
        log.info("notification channel live (watermark %s)", last)
        if self._status is not None:
            self._status.set(notifications="live")  # type: ignore[attr-defined]
        while self._running:
            await asyncio.sleep(self._interval)
            try:
                fresh, last = await asyncio.to_thread(self._store.read_new, last)
            except Exception as exc:
                log.warning("notification poll failed: %s", exc)
                continue
            for n in fresh:
                await self._handle(n)

    def stop(self) -> None:
        self._running = False

    # A message this short IS its own summary. The model is still asked to
    # CLASSIFY it (so promotional noise is still filtered), but its wording
    # is not used: asked to compress "ok see you at 7", a small model
    # invents. Only longer content earns a model-written summary.
    VERBATIM_CHARS = 70

    STALE_CARD_S = 120.0
    """Older than this and a card no longer interrupts the lens.

    Two minutes is deliberately generous: today's median delivery lag was
    1.2s, so nothing arriving in the normal course is affected, and only
    the backlog that drains after a reconnect is held back.
    """

    async def _handle(self, n: Notification) -> bool:
        raw = (n.body or n.subtitle or n.title).strip()
        # macOS elides the banner body it renders, and the Accessibility
        # watcher can only read what is on screen — so a long message arrives
        # ALREADY cut, and no display budget can recover it. Where the Mac
        # can read the authoritative store (chat.db for Messages), ask it for
        # the whole text before anything downstream summarises or measures.
        if self._enrich_body is not None and raw.rstrip().endswith(("…", "...")):
            channel_hint = humanize_app(n.app)
            try:
                full = await asyncio.to_thread(
                    self._enrich_body, channel_hint, raw, n.delivered_unix,
                )
            except Exception as exc:
                log.info("notification enrichment failed (%s)", type(exc).__name__)
                full = None
            if full:
                log.info(
                    "notification enriched channel=%s preview_chars=%d full_chars=%d",
                    channel_hint, len(raw), len(full),
                )
                raw = full.strip()
        level, summary = Urgency.NOTIFY, raw[:120]
        short = len(raw) <= self.VERBATIM_CHARS
        if self._triage is not None:
            age = (humanize_age(time.time() - n.delivered_unix)
                   if n.delivered_unix > 0 else "unknown")
            try:
                async with asyncio.timeout(self._triage_timeout_s):
                    reply = await self._triage.reply(TRIAGE_PROMPT.format(
                        app=n.app, age=age, title=n.title, subtitle=n.subtitle,
                        body=n.body,
                    ))
            except TimeoutError:
                reply = None
                log.warning(
                    "notification triage timed out channel=%s chars=%d",
                    humanize_app(n.app),
                    len(raw),
                )
            except Exception as exc:
                reply = None
                log.warning(
                    "notification triage failed channel=%s error=%s chars=%d",
                    humanize_app(n.app),
                    type(exc).__name__,
                    len(raw),
                )
            parsed = parse_triage(reply or "")
            if parsed is not None:
                level = parsed[0]
                # Trust the model's WORDS only for long content it actually
                # needed to compress, and never when it echoed the prompt.
                if not short and not looks_like_echo(parsed[1]) and parsed[1]:
                    summary = parsed[1]
            # Unparseable triage falls back to NOTIFY with the raw title —
            # a flaky judge must never silently hide notifications.

        channel = humanize_app(n.app)
        # For a person-to-person channel the TITLE is the sender, which is
        # what the wearer needs to see; for app alerts the app name is.
        source = n.title if channel in PERSON_CHANNELS else channel
        if level is Urgency.URGENT:
            # Urgency must be corroborated by the text itself, not asserted
            # by a 3B model that marked routine texts URGENT in testing.
            if is_systemish(n.app, n.title) or not corroborates_urgency(raw):
                level = Urgency.NOTIFY
        if not summary.strip():
            summary = n.body or n.title

        # A card interrupts whatever is on the lens. That is worth doing for
        # something that just happened and never for something that did not:
        # measured on the wearer's glasses 2026-09-08, 15 of 109 delivered
        # cards were over a minute stale and the worst was 3.15 DAYS old,
        # because a backlog drains up to 50 at a time (notification_mux)
        # whenever the link returns. Those are what "a different screen
        # replaces it" looks like from the wearer's side.
        #
        # Staleness is not suppression: the card still lands in the log and
        # the notifications tray, where the wearer can read it on purpose.
        # It just does not seize a screen minutes or days after the fact.
        age_s = time.time() - n.delivered_unix if n.delivered_unix > 0 else 0.0
        if age_s > self.STALE_CARD_S:
            log.info(
                "notification disposition=too_stale_to_interrupt channel=%s age_s=%.0f",
                channel, age_s,
            )
            return False

        card = self._policy.admit(Card(
            source=source, channel=channel, summary=summary, body=raw, urgency=level,
            event_class=classify_event_class(n.app, n.title), origin_id=n.native_id,
        ))
        if card is None:
            log.info(
                "notification disposition=suppressed urgency=%s channel=%s chars=%d",
                level.name,
                channel,
                len(summary),
            )
            return False
        # The card itself is delivered; this rendering is only for the log
        # line and the status mirror.
        rendered = card.render()
        log.info(
            "notification disposition=enqueued channel=%s latency_s=%.1f chars=%d",
            channel,
            time.time() - n.delivered_unix,
            len(rendered),
        )
        delivery = self._deliver(card)
        if inspect.isawaitable(delivery):
            await delivery
        if self._status is not None:
            self._status.set(last_card=rendered)  # type: ignore[attr-defined]
            self._status.bump("cards")  # type: ignore[attr-defined]
        return True
