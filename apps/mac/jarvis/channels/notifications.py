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
import logging
import plistlib
import sqlite3
import time
from collections.abc import Callable
from typing import Any, Protocol
from dataclasses import dataclass
from pathlib import Path

from ..core.interfaces import ReplyEngine
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
    return Notification(rec_id, app, title, subtitle, body, when)


class NotificationStore:
    """Read-only view of the usernoted db. Injectable path for tests."""

    def __init__(self, db_path: Path = DEFAULT_DB) -> None:
        self._path = db_path

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
    "App: {app}\nTitle: {title}\nSubtitle: {subtitle}\nBody: {body}"
)


def parse_triage(reply: str) -> tuple[Urgency, str] | None:
    head = reply.strip().splitlines()[0] if reply.strip() else ""
    for name, level in (("URGENT", Urgency.URGENT), ("NOTIFY", Urgency.NOTIFY),
                        ("IGNORE", Urgency.IGNORE)):
        if head.upper().startswith(name):
            summary = head[len(name):].lstrip(" —-–:").strip()
            return level, summary
    return None


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
        deliver: Callable[[str], None],
        poll_interval_s: float = 2.0,
        status: object | None = None,
        policy: DeliveryPolicy | None = None,
    ) -> None:
        self._store = store
        self._triage = triage
        self._deliver = deliver
        self._interval = poll_interval_s
        self._status = status
        self._policy = policy or DeliveryPolicy()
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

    async def _handle(self, n: Notification) -> None:
        raw = (n.body or n.subtitle or n.title).strip()
        level, summary = Urgency.NOTIFY, raw[:120]
        short = len(raw) <= self.VERBATIM_CHARS
        if self._triage is not None:
            reply = await self._triage.reply(TRIAGE_PROMPT.format(
                app=n.app, title=n.title, subtitle=n.subtitle, body=n.body,
            ))
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
        source = n.title if channel in ("Messages", "Mail", "Slack", "Discord") else channel
        if level is Urgency.URGENT:
            # Urgency must be corroborated by the text itself, not asserted
            # by a 3B model that marked routine texts URGENT in testing.
            if is_systemish(n.app, n.title) or not corroborates_urgency(raw):
                level = Urgency.NOTIFY
        if not summary.strip():
            summary = n.body or n.title

        card = self._policy.admit(Card(source=source, channel=channel,
                                       summary=summary, urgency=level))
        if card is None:
            log.info("card suppressed (%s): [%s] %s", level.name, channel, summary)
            return
        rendered = card.render()
        log.info("card delivered (%.1fs after delivery): %s",
                 time.time() - n.delivered_unix, rendered.replace("\n", " / "))
        if self._status is not None:
            self._status.set(last_card=rendered)  # type: ignore[attr-defined]
            self._status.bump("cards")  # type: ignore[attr-defined]
        self._deliver(rendered)
