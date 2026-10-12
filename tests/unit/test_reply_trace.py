"""scripts/reply_trace.py reads brain.log + g2-diagnostics.json and says
where a spoken Messages reply stopped, without printing any of its words."""

from __future__ import annotations

from datetime import datetime

from scripts.reply_trace import build_report, parse_since, render, verdict

G = "joat.ble.hub_gateway"
R = "joat.services.message_reply"
P = "joat.services.prompted_input"
O = "joat.services.orchestrator"


def line(stamp: str, logger: str, message: str, level: str = "INFO") -> str:
    return f"2026-10-08 {stamp} {level:<7} {logger}: {message}"


# The shape brain.log had on 2026-10-08: ~15 s links, code 1005, forever.
WATCHDOG_LOOP = [
    line("10:00:16,482", G, "plugin connected"),
    line("10:00:16,495", G, "device g2 / S200TEST000001"),
    line("10:00:31,657", G, "plugin disconnected code=1005 reason='' after=15.2s replaced=False"),
    line("10:00:32,227", G, "plugin connected"),
    line("10:00:32,236", G, "device g2 / S200TEST000001"),
    line("10:00:47,346", G, "dropping unparseable uplink: g2 diagnostic containerId is invalid", "WARNING"),
    line("10:00:47,359", G, "plugin disconnected code=1005 reason='' after=15.1s replaced=False"),
    line("10:01:00,437", G, "plugin connected"),
    line("10:01:15,597", G, "plugin disconnected code=1005 reason='' after=15.2s replaced=False"),
    line("10:01:16,190", G, "plugin connected"),
    line("10:01:31,353", G, "plugin disconnected code=1005 reason='' after=15.1s replaced=False"),
]

# A healthy reply: thread opened, Reply tapped, question asked, words shown,
# consent requested, tap, submitted.
HEALTHY_REPLY = [
    line("13:48:22,774", G, "plugin connected"),
    line("13:48:22,780", G, "device g2 / S200TEST000001"),
    line("13:50:16,332", R, "message reply trace=hud-9fe779e7 stage=selected recipient=529e9f20d5"),
    line("13:50:40,100", R, "message reply trace=hud-9fe779e7 stage=reply-prompt-armed recipient=529e9f20d5"),
    line("13:50:43,000", O, "VADTRACE emit bytes=65600 t=1.0"),
    line("13:50:45,000", O, "transcript received chars=31"),
    line("13:50:45,100", R, "message reply trace=83d216d9 stage=review-requested recipient=529e9f20d5 chars=31"),
    line("13:50:45,200", R, "message reply trace=83d216d9 stage=confirmation-requested recipient=529e9f20d5 chars=31"),
    line("13:50:49,000", R, "message reply trace=83d216d9 stage=submitted delivery=unverified"),
    line("13:57:07,921", G, "plugin disconnected code=1005 reason='' after=525.1s replaced=False"),
]

TRACE = {
    "schema": "x",
    "events": [
        {"type": "hud_surface", "stage": "first_frame", "surface": "visual", "outcome": "success",
         "recordedAtMs": int(datetime(2026, 10, 8, 13, 48, 23).timestamp() * 1000), "sequence": 1},
        {"type": "prompt_stage", "owner": "messages", "stage": "presented",
         "recordedAtMs": int(datetime(2026, 10, 8, 13, 50, 40, 200000).timestamp() * 1000), "sequence": 2},
        {"type": "prompt_stage", "owner": "messages", "stage": "painted",
         "recordedAtMs": int(datetime(2026, 10, 8, 13, 50, 40, 600000).timestamp() * 1000), "sequence": 3},
        {"type": "prompt_stage", "owner": "messages", "stage": "mic_ready",
         "recordedAtMs": int(datetime(2026, 10, 8, 13, 50, 41).timestamp() * 1000), "sequence": 4},
        {"type": "prompt_stage", "owner": "messages", "stage": "acknowledged",
         "recordedAtMs": int(datetime(2026, 10, 8, 13, 50, 41, 100000).timestamp() * 1000), "sequence": 5},
        {"type": "prompt_stage", "owner": "messages", "stage": "retired",
         "recordedAtMs": int(datetime(2026, 10, 8, 13, 50, 45, 300000).timestamp() * 1000), "sequence": 6},
        {"type": "messages_confirmation", "outcome": "accepted",
         "recordedAtMs": int(datetime(2026, 10, 8, 13, 50, 48, 500000).timestamp() * 1000), "sequence": 7},
    ],
}


def test_the_watchdog_loop_is_the_first_thing_the_verdict_says() -> None:
    report = build_report(WATCHDOG_LOOP, None, since=None)
    assert len(report.connections) == 4
    assert all(conn.watchdog_shaped for conn in report.connections)
    assert report.unparseable["g2 diagnostic containerId is invalid"] == 1
    first = verdict(report)[0]
    assert "4 of 4 connections died about 15 s" in first
    assert "downlink watchdog" in first
    assert "no spoken reply can survive" in first
    text = render(report, connections=20)
    assert text.count("<- watchdog-shaped") == 4
    assert "diagnostic refused: g2 diagnostic containerId is invalid" in text
    # The device serial never reaches the report; the model does.
    assert "S200TEST000001" not in text
    assert " g2 " in text


def test_a_healthy_reply_is_told_stage_by_stage_from_both_files() -> None:
    report = build_report(HEALTHY_REPLY, TRACE, since=None)
    assert [stage for _, stage, _ in report.reply_stages] == [
        "selected", "reply-prompt-armed", "review-requested", "confirmation-requested", "submitted",
    ]
    assert [stage for _, _, stage in report.prompt_stages] == [
        "presented", "painted", "mic_ready", "acknowledged", "retired",
    ]
    assert report.confirmations == [(datetime(2026, 10, 8, 13, 50, 48, 500000), "glasses: accepted")]
    assert report.voice == {"utterances": 1, "transcripts": 1}
    lines = verdict(report)
    assert not any("watchdog" in item for item in lines)
    assert "A message was submitted to Messages." in lines
    assert any(item.startswith("Question steps on the glasses: presented -> painted") for item in lines)
    text = render(report, connections=20)
    # Glasses-side steps are placed under the connection that was open.
    assert "      13:50:40  glasses: question presented (messages)" in text
    assert "      13:50:48  glasses: consent accepted" in text
    # Content-free: the recipient digest and body length are the only details,
    # and the recipient digest is elided.
    assert "529e9f20d5" not in text
    assert "recipient=…" in text


def test_a_thread_left_without_reply_is_not_called_a_lost_tap() -> None:
    lines = [
        line("13:48:22,774", G, "plugin connected"),
        line("13:50:16,332", R, "message reply trace=hud-1 stage=selected recipient=abc"),
        line("13:52:26,664", R, "message reply trace=hud-1 stage=selection-cleared reason=list"),
    ]
    report = build_report(lines, None, since=None)
    said = verdict(report)
    assert any("opened and left again (reason=list) before Reply was tapped" in item for item in said)
    assert not any("did not reach the Mac" in item for item in said)


def test_an_armed_question_without_glasses_steps_points_at_the_diagnostics_toggle() -> None:
    lines = [
        line("13:48:22,774", G, "plugin connected"),
        line("13:50:16,332", R, "message reply trace=hud-1 stage=selected recipient=abc"),
        line("13:50:40,100", R, "message reply trace=hud-1 stage=reply-prompt-armed recipient=abc"),
        line("13:50:50,100", R, "message reply trace=hud-1 stage=reply-prompt-cancelled recipient=abc"),
        line("13:50:50,200", P, "prompted answer not taken owner=messages reason=other_speaker"),
    ]
    report = build_report(lines, {"events": []}, since=None)
    said = verdict(report)
    assert any("Record G2 diagnostics' was off" in item for item in said)
    assert any("asked and then cancelled before an answer" in item for item in said)
    assert any("messages:other_speaker x1" in item for item in said)
    assert any(item.startswith("other_speaker:") for item in said)


def test_a_consent_with_no_decision_is_named() -> None:
    lines = [
        line("01:13:30,704", R, "message reply trace=hud-2 stage=selected recipient=e32"),
        line("01:13:50,000", R, "message reply trace=hud-2 stage=reply-prompt-armed recipient=e32"),
        line("01:14:00,813", R, "message reply trace=83d2 stage=confirmation-requested recipient=e32 chars=44"),
        line("01:14:34,135", R, "message reply trace=hud-2 stage=selection-cleared reason=capability-open"),
    ]
    report = build_report(lines, None, since=None)
    said = verdict(report)
    assert said[0].startswith("No plugin connect or disconnect in this window")
    assert any("asked for consent but no decision came back" in item for item in said)


def test_since_cuts_old_lines_and_accepts_relative_and_iso_forms() -> None:
    now = datetime(2026, 10, 8, 14, 0, 0)
    assert parse_since("2h", now) == datetime(2026, 10, 8, 12, 0, 0)
    assert parse_since("30m", now) == datetime(2026, 10, 8, 13, 30, 0)
    assert parse_since("1d", now) == datetime(2026, 10, 7, 14, 0, 0)
    assert parse_since("2026-10-08T13:00:00", now) == datetime(2026, 10, 8, 13, 0, 0)
    assert parse_since(None, now) is None
    report = build_report(WATCHDOG_LOOP + HEALTHY_REPLY, TRACE, since=datetime(2026, 10, 8, 13, 0, 0))
    assert len(report.connections) == 1
    assert report.unparseable == {}
    assert len(report.prompt_stages) == 5


def test_malformed_lines_and_trace_are_skipped_not_fatal() -> None:
    lines = ["I agree vectors need source-level protection", "", "Fetching 6 files: 100%"] + HEALTHY_REPLY[:3]
    report = build_report(lines, {"events": "nope"}, since=None)
    assert len(report.connections) == 1
    assert report.prompt_stages == []
    report = build_report(lines, {"events": [{"type": "prompt_stage"}, 3, {"recordedAtMs": "x"}]}, since=None)
    assert report.prompt_stages == []
