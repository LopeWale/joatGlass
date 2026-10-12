#!/usr/bin/env python3
"""Where did a spoken Messages reply stop on the glasses?

Reads the brain log and the G2 diagnostics the Mac keeps, and prints, for
the most recent plugin connections, the one story that spans both: how
long each link lived and how it ended, which Messages reply reached which
stage, each step the question reached on the glasses (``prompt_stage``),
why a spoken answer was turned down, and what the voice path saw. It ends
with a verdict in plain words.

Content-free by construction: it prints durations, stage names, reason
codes and counts. It never prints message bodies, recipients, question
text, transcripts, or the device serial (the log line that carries it is
reduced to the model).

    .venv/bin/python scripts/reply_trace.py              # last 20 connections
    .venv/bin/python scripts/reply_trace.py --since 2h   # the last two hours
    .venv/bin/python scripts/reply_trace.py --connections 5

Turn on "Record G2 diagnostics" before the run on the glasses, or the
``prompt_stage`` section is empty by design (2026-10-11: off means off).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_LOG = Path.home() / "Library/Logs/joat/brain.log"
DEFAULT_TRACE = Path.home() / "Library/Application Support/JOAT/g2-diagnostics.json"

# apps/hub-plugin/src/MacLink.ts ``downlinkHandlerTimeoutMs``; the Mac's
# twin is ``PLUGIN_DOWNLINK_WATCHDOG_S`` in joat/ble/hub_gateway.py.
WATCHDOG_S = 15.0
WATCHDOG_TOLERANCE_S = 3.0

_LINE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}) (?P<time>\d{2}:\d{2}:\d{2}),(?P<ms>\d{3}) "
    r"(?P<level>[A-Z]+)\s+(?P<logger>[\w.]+): (?P<message>.*)$"
)
_DISCONNECT = re.compile(
    r"plugin disconnected code=(?P<code>\S+) reason=(?P<reason>'.*?'|\".*?\") "
    r"after=(?P<after>[\d.]+)s replaced=(?P<replaced>\w+)"
)
_DEVICE = re.compile(r"^device (?P<model>\S+) / \S+$")
_REPLY = re.compile(r"message reply trace=(?P<trace>\S+) stage=(?P<stage>[\w-]+)(?P<rest>.*)$")
_REFUSED = re.compile(r"prompted answer not taken owner=(?P<owner>\w+) reason=(?P<reason>\w+)")
_TRANSCRIPT = re.compile(r"transcript received chars=(?P<chars>\d+)")
_IGNORED = re.compile(r"ignored non-wearer utterance bytes=\d+ source=(?P<source>\w+) roles=(?P<roles>[\w,]+)")
_UNPARSEABLE = re.compile(r"dropping unparseable uplink: (?P<why>.*)$")
_CONFIRM = re.compile(r"confirm \S+ (?P<outcome>declined|timed out)(?: — (?P<why>.*))?$")
_DURATION = re.compile(r"^(?P<n>\d+)(?P<unit>[smhd])$")


@dataclass(slots=True)
class Connection:
    opened: datetime
    closed: datetime | None = None
    lifetime_s: float | None = None
    code: str | None = None
    model: str | None = None
    events: list[tuple[datetime, str]] = field(default_factory=list)

    @property
    def watchdog_shaped(self) -> bool:
        return (
            self.lifetime_s is not None
            and abs(self.lifetime_s - WATCHDOG_S) <= WATCHDOG_TOLERANCE_S
            and self.code in ("1005", "1006")
        )


@dataclass(slots=True)
class Report:
    connections: list[Connection]
    reply_stages: list[tuple[datetime, str, str]]  # when, stage, detail
    refusals: Counter[str]
    prompt_stages: list[tuple[datetime, str, str]]  # when, owner, stage
    confirmations: list[tuple[datetime, str]]
    voice: Counter[str]
    unparseable: Counter[str]
    watchdog_warnings: int
    window_start: datetime | None
    window_end: datetime | None


def parse_since(value: str | None, now: datetime) -> datetime | None:
    """``2h``, ``30m``, ``1d`` or an ISO timestamp; None means no cut."""
    if value is None:
        return None
    match = _DURATION.match(value.strip())
    if match:
        n = int(match.group("n"))
        unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[match.group("unit")]
        return now - timedelta(**{unit: n})
    return datetime.fromisoformat(value)


def _stamp(match: re.Match[str]) -> datetime:
    return datetime.fromisoformat(f"{match.group('date')}T{match.group('time')}.{match.group('ms')}000")


def parse_log(lines: list[str], *, since: datetime | None = None) -> Report:
    connections: list[Connection] = []
    open_conn: Connection | None = None
    reply_stages: list[tuple[datetime, str, str]] = []
    refusals: Counter[str] = Counter()
    confirmations: list[tuple[datetime, str]] = []
    voice: Counter[str] = Counter()
    unparseable: Counter[str] = Counter()
    watchdog_warnings = 0
    first: datetime | None = None
    last: datetime | None = None
    for raw in lines:
        match = _LINE.match(raw.rstrip("\n"))
        if match is None:
            continue
        when = _stamp(match)
        if since is not None and when < since:
            continue
        first = when if first is None else first
        last = when
        logger, message = match.group("logger"), match.group("message")
        if logger.endswith("hub_gateway"):
            if message == "plugin connected":
                open_conn = Connection(opened=when)
                connections.append(open_conn)
                continue
            disconnect = _DISCONNECT.search(message)
            if disconnect:
                target = open_conn or Connection(opened=when)
                if open_conn is None:
                    connections.append(target)
                target.closed = when
                target.lifetime_s = float(disconnect.group("after"))
                target.code = disconnect.group("code")
                open_conn = None
                continue
            device = _DEVICE.match(message)
            if device and open_conn is not None:
                open_conn.model = device.group("model")
                continue
            if "downlink watchdog is firing" in message:
                watchdog_warnings += 1
                _note(open_conn, when, "watchdog loop named in the log")
                continue
            bad = _UNPARSEABLE.search(message)
            if bad:
                unparseable[bad.group("why")] += 1
                _note(open_conn, when, f"diagnostic refused: {bad.group('why')}")
                continue
            confirm = _CONFIRM.search(message)
            if confirm:
                detail = confirm.group("outcome") + (f" — {confirm.group('why')}" if confirm.group("why") else "")
                confirmations.append((when, detail))
                _note(open_conn, when, f"consent {detail}")
                continue
        elif logger.endswith("message_reply") or logger.endswith("message_ui"):
            reply = _REPLY.search(message)
            if reply:
                detail = _reason_only(reply.group("rest"))
                reply_stages.append((when, reply.group("stage"), detail))
                _note(open_conn, when, f"reply {reply.group('stage')}{(' ' + detail) if detail else ''}")
            elif logger.endswith("message_ui") and match.group("level") == "WARNING":
                _note(open_conn, when, "Messages UI: " + _reason_only(message))
        elif logger.endswith("prompted_input"):
            refused = _REFUSED.search(message)
            if refused:
                refusals[f"{refused.group('owner')}:{refused.group('reason')}"] += 1
                _note(open_conn, when, f"answer refused owner={refused.group('owner')} reason={refused.group('reason')}")
        elif logger.endswith("orchestrator"):
            if "VADTRACE emit" in message:
                voice["utterances"] += 1
            elif _TRANSCRIPT.search(message):
                voice["transcripts"] += 1
            elif _IGNORED.search(message):
                voice["ignored as another speaker"] += 1
            elif message.startswith("audio gap"):
                voice["audio gaps"] += 1
            elif message.startswith("audio stream changed"):
                voice["stream restarts"] += 1
            elif message.startswith("partial transcript"):
                voice["partial failures"] += 1
    return Report(
        connections=connections, reply_stages=reply_stages, refusals=refusals,
        prompt_stages=[], confirmations=confirmations, voice=voice,
        unparseable=unparseable, watchdog_warnings=watchdog_warnings,
        window_start=first, window_end=last,
    )


def _note(conn: Connection | None, when: datetime, text: str) -> None:
    if conn is not None:
        conn.events.append((when, text))


def _reason_only(rest: str) -> str:
    """Keep ``reason=…``/``recipient=…``-style tokens; drop anything else."""
    kept = []
    for token in rest.split():
        key, sep, value = token.partition("=")
        if sep and key in ("reason", "recipient", "chars", "delivery", "owner", "field"):
            kept.append(f"{key}={value}" if key != "recipient" else "recipient=…")
    return " ".join(kept)


def attach_prompt_stages(report: Report, trace: dict[str, Any] | None, *, since: datetime | None) -> None:
    """``prompt_stage`` and ``messages_confirmation`` events from the Mac's
    diagnostics export, placed on the connection that was open at the time."""
    if not isinstance(trace, dict):
        return
    events = trace.get("events")
    if not isinstance(events, list):
        return
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("recordedAtMs"), (int, float)):
            continue
        when = datetime.fromtimestamp(float(event["recordedAtMs"]) / 1000.0)
        if since is not None and when < since:
            continue
        kind = event.get("type")
        if kind == "prompt_stage":
            owner, stage = str(event.get("owner", "?")), str(event.get("stage", "?"))
            report.prompt_stages.append((when, owner, stage))
            _note(_connection_at(report, when), when, f"glasses: question {stage} ({owner})")
        elif kind == "messages_confirmation":
            outcome = str(event.get("outcome", "?"))
            report.confirmations.append((when, f"glasses: {outcome}"))
            _note(_connection_at(report, when), when, f"glasses: consent {outcome}")
        elif kind == "tile_upload" and event.get("outcome") in ("failed", "error"):
            _note(
                _connection_at(report, when), when,
                f"glasses: image write to container {event.get('containerId')} {event.get('outcome')}",
            )


def _connection_at(report: Report, when: datetime) -> Connection | None:
    for conn in reversed(report.connections):
        if conn.opened <= when and (conn.closed is None or when <= conn.closed):
            return conn
    return None


def verdict(report: Report) -> list[str]:
    lines: list[str] = []
    conns = report.connections
    if not conns:
        lines.append(
            "No plugin connect or disconnect in this window (the link was either up the whole time or never made)."
        )
    else:
        shaped = [c for c in conns if c.watchdog_shaped]
        if len(shaped) >= 3 and len(shaped) >= len(conns) // 2:
            lines.append(
                f"{len(shaped)} of {len(conns)} connections died about {WATCHDOG_S:.0f} s after connecting with no "
                "close frame: the plugin's downlink watchdog is dropping the link because one Mac->plugin message "
                "is not handled in time on the glasses. Every reconnect cancels an open question, so no spoken reply "
                "can survive. Fix the hung write first; nothing below can be judged until the link holds."
            )
    stages = [stage for _, stage, _ in report.reply_stages]
    if not stages:
        lines.append(
            "No Messages reply reached the Mac (no 'stage=selected'): the tap on the thread or on Reply never "
            "arrived. Check the link above and that the Messages page was open on the glasses."
        )
    else:
        lines.append("Reply stages seen, in order: " + " -> ".join(stages) + ".")
        armed = "reply-prompt-armed" in stages or "compose-selected" in stages
        if not armed:
            left = [detail for _, stage, detail in report.reply_stages if stage == "selection-cleared"]
            if left:
                lines.append(
                    f"A conversation was opened and left again ({left[-1] or 'no reason'}) before Reply was tapped: "
                    "no question was asked."
                )
            else:
                lines.append(
                    "The conversation was selected but the question was never armed: the tap on Reply (or the word "
                    "'reply') did not reach the Mac, or the plugin refused the question (see the glasses stages)."
                )
        if "reply-prompt-cancelled" in stages and "review-requested" not in stages:
            lines.append(
                "The question was asked and then cancelled before an answer: the plugin retired it (link drop, "
                "timeout, or a refused paint or microphone — see the glasses stages)."
            )
        if "draft-rejected" in stages:
            lines.append("The spoken draft was rejected by the Mac's check before consent.")
        if "confirmation-requested" in stages and "submitted" not in stages and not report.confirmations:
            lines.append(
                "The Mac asked for consent but no decision came back: the consent card did not show, or it was "
                "not answered in time."
            )
        if "send-failed" in stages:
            lines.append("Consent was given but Messages did not accept the send.")
        if "submitted" in stages:
            lines.append("A message was submitted to Messages.")
    glasses = [stage for _, _, stage in report.prompt_stages]
    if glasses:
        lines.append("Question steps on the glasses: " + " -> ".join(glasses) + ".")
        if "paint_refused" in glasses:
            lines.append("The question page could not be painted: the HUD refused the page while another write was in flight.")
        if "mic_refused" in glasses:
            lines.append("The glasses microphone did not open for the question.")
        if "ack_refused" in glasses:
            lines.append("The microphone opened but the acknowledgement never reached the Mac on the right connection.")
    elif "reply-prompt-armed" in stages:
        lines.append(
            "No glasses-side question steps were recorded: 'Record G2 diagnostics' was off, or the trace file is "
            "older than the run."
        )
    if report.refusals:
        detail = ", ".join(f"{key} x{count}" for key, count in report.refusals.most_common())
        lines.append(f"Spoken answers turned down by the Mac: {detail}.")
        if any(key.endswith(":other_speaker") for key in report.refusals):
            lines.append(
                "other_speaker: the glasses tagged the words as another person's; speak with the glasses on and "
                "nobody else talking."
            )
        if any(key.endswith(":started_before_question") for key in report.refusals):
            lines.append(
                "started_before_question: speech began before the question was armed; wait for the question card, "
                "then speak."
            )
    if report.voice:
        if report.voice["utterances"] and not report.voice["transcripts"]:
            lines.append("Speech was heard but nothing was transcribed: check whisper serve in the log.")
        if report.voice["ignored as another speaker"] > report.voice["transcripts"]:
            lines.append("Most speech was tagged as another speaker's and dropped.")
    if report.unparseable:
        lines.append(
            "The Mac refused some of the glasses' diagnostics ("
            + "; ".join(f"{why} x{n}" for why, n in report.unparseable.most_common(3))
            + "): restart the brain from the current revision so they are recorded."
        )
    return lines


def _section(out: list[str], title: str, items: list[str], empty: str = "  none") -> None:
    out.append(f"\n{title}")
    out.extend(items if items else [empty])


def render(report: Report, *, connections: int) -> str:
    out: list[str] = []
    if report.window_start and report.window_end:
        out.append(f"Window: {report.window_start:%Y-%m-%d %H:%M:%S} -> {report.window_end:%Y-%m-%d %H:%M:%S}")
    shown = report.connections[-connections:]
    out.append(f"\nLink ({len(report.connections)} connections, last {len(shown)} shown):")
    for conn in shown:
        life = f"{conn.lifetime_s:7.1f} s" if conn.lifetime_s is not None else "   open  "
        flag = "  <- watchdog-shaped" if conn.watchdog_shaped else ""
        model = f" {conn.model}" if conn.model else ""
        out.append(f"  {conn.opened:%m-%d %H:%M:%S}  {life}  code={conn.code or '-':>5}{model}{flag}")
        for when, text in conn.events:
            out.append(f"      {when:%H:%M:%S}  {text}")
    if not shown:
        out.append("  none")
    if report.watchdog_warnings:
        out.append(f"  The log named the watchdog loop {report.watchdog_warnings} time(s).")
    _section(out, "Messages reply stages:", [
        f"  {when:%m-%d %H:%M:%S}  {stage}{(' ' + detail) if detail else ''}"
        for when, stage, detail in report.reply_stages
    ])
    _section(out, "Question steps on the glasses (prompt_stage):", [
        f"  {when:%m-%d %H:%M:%S}  {owner}: {stage}" for when, owner, stage in report.prompt_stages
    ])
    _section(out, "Spoken answers the Mac turned down:", [
        f"  {key}: {count}" for key, count in report.refusals.most_common()
    ])
    _section(out, "Consent decisions:", [
        f"  {when:%m-%d %H:%M:%S}  {detail}" for when, detail in report.confirmations
    ])
    _section(out, "Voice path:", [
        f"  {key}: {count}" for key, count in sorted(report.voice.items())
    ], empty="  nothing heard")
    if report.unparseable:
        _section(out, "Diagnostics the Mac refused:", [
            f"  {why}: {count}" for why, count in report.unparseable.most_common()
        ])
    _section(out, "Verdict:", [f"  - {line}" for line in verdict(report)])
    return "\n".join(out)


def build_report(
    log_lines: list[str], trace: dict[str, Any] | None, *, since: datetime | None,
) -> Report:
    report = parse_log(log_lines, since=since)
    attach_prompt_stages(report, trace, since=since)
    report.prompt_stages.sort(key=lambda item: item[0])
    report.confirmations.sort(key=lambda item: item[0])
    for conn in report.connections:
        conn.events.sort(key=lambda item: item[0])
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--trace", type=Path, default=DEFAULT_TRACE)
    parser.add_argument("--since", help="2h, 30m, 1d, or an ISO timestamp (default: whole log)")
    parser.add_argument("--connections", type=int, default=20, help="connections to list (default 20)")
    args = parser.parse_args(argv)
    if not args.log.exists():
        print(f"no brain log at {args.log}", file=sys.stderr)
        return 2
    since = parse_since(args.since, datetime.now())
    lines = args.log.read_text(encoding="utf-8", errors="replace").splitlines()
    trace: dict[str, Any] | None = None
    if args.trace.exists():
        try:
            loaded = json.loads(args.trace.read_text(encoding="utf-8"))
            trace = loaded if isinstance(loaded, dict) else None
        except (OSError, ValueError):
            trace = None
    report = build_report(lines, trace, since=since)
    print(render(report, connections=max(1, args.connections)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
