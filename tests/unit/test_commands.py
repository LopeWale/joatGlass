"""Production voice-command composition and precedence."""

from pathlib import Path

from joat.audio.router import AudioRouter
from joat.core.interfaces import (
    AudioFrame,
    AudioFrameCallback,
    AudioFrameSource,
    SttEngine,
    Transcript,
)
from joat.persistence.store import DurableStore
from joat.persistence.voice_notes import VoiceNoteStore
from joat.services.commands import VoiceCommandRouter
from joat.services.models import ModelRegistry
from joat.services.note_insights import (
    NoteInsightCommandController,
    NoteInsightsService,
)
from joat.services.voice_note_capture import VoiceNoteCaptureController


class RecordingReplies:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def handle_command(self, text: str) -> str | None:
        self.seen.append(text)
        return None

    def is_compose_armed(self) -> bool:
        return False


class ArmedComposeReplies(RecordingReplies):
    def is_compose_armed(self) -> bool:
        return True

    async def handle_command(self, text: str) -> str | None:
        self.seen.append(text)
        return "Recipient Adele selected. Say the message."


class RecordingProductivity:
    def __init__(self, result: str | None = None) -> None:
        self.result = result
        self.seen: list[str] = []

    async def handle_command(self, text: str) -> str | None:
        self.seen.append(text)
        return self.result


class TestVoiceCommandRouter:
    async def test_greeting_is_claimed_before_side_effect_commands(self) -> None:
        replies = RecordingReplies()
        commands = VoiceCommandRouter(replies, ModelRegistry([]))

        result = await commands("Good morning.")
        assert result == "Good morning. How can I help?"
        assert getattr(result, "presentation", None) == "conversation"
        assert replies.seen == []

    async def test_armed_new_message_compose_claims_a_greeting_shaped_body_before_greeting(self) -> None:
        replies = ArmedComposeReplies()
        commands = VoiceCommandRouter(replies, ModelRegistry([]))

        assert await commands("Good morning") == "Recipient Adele selected. Say the message."
        assert replies.seen == ["Good morning"]

    async def test_substantive_greeting_falls_through(self) -> None:
        replies = RecordingReplies()
        commands = VoiceCommandRouter(replies, ModelRegistry([]))

        assert await commands("Good morning, what is on my calendar?") is None
        assert replies.seen == ["Good morning, what is on my calendar?"]

    async def test_productivity_claims_exact_intent_before_model_controls(self) -> None:
        replies = RecordingReplies()
        productivity = RecordingProductivity("Calendar event cancelled.")
        commands = VoiceCommandRouter(
            replies,
            ModelRegistry([]),
            productivity=productivity,
        )

        text = "schedule Review on 2026-09-01 at 09:30 for 30 minutes in America/Chicago"
        assert await commands(text) == "Calendar event cancelled."
        assert replies.seen == [text]
        assert productivity.seen == [text]

    async def test_quick_note_claims_explicit_capture_before_productivity(self) -> None:
        replies = RecordingReplies()
        notes = RecordingProductivity("Saved QuickNote: exact")
        productivity = RecordingProductivity()
        commands = VoiceCommandRouter(
            replies,
            ModelRegistry([]),
            productivity=productivity,
            notes=notes,
        )

        result = await commands("take a note exact")
        assert result == "Saved QuickNote: exact"
        assert getattr(result, "presentation", None) == "receipt"
        assert notes.seen == ["take a note exact"]
        assert productivity.seen == []

    async def test_voice_note_control_is_claimed_before_quick_note(self) -> None:
        replies = RecordingReplies()
        voice_notes = RecordingProductivity("Recording Voice Note: review")
        notes = RecordingProductivity("wrong controller")
        commands = VoiceCommandRouter(
            replies,
            ModelRegistry([]),
            notes=notes,
            voice_notes=voice_notes,
        )

        assert await commands("start a voice note review") == (
            "Recording Voice Note: review"
        )
        assert voice_notes.seen == ["start a voice note review"]
        assert notes.seen == []

    async def test_hermes_calendar_proposal_is_claimed_before_general_inference(self) -> None:
        replies = RecordingReplies()
        productivity = RecordingProductivity()
        hermes = RecordingProductivity("Calendar event cancelled.")
        commands = VoiceCommandRouter(
            replies,
            ModelRegistry([]),
            productivity=productivity,
            hermes=hermes,
        )

        text = "ask Hermes to draft a calendar event for review tomorrow"
        assert await commands(text) == "Calendar event cancelled."
        assert hermes.seen == [text]

    async def test_note_insights_claim_between_recorder_and_text_notes(self) -> None:
        replies = RecordingReplies()
        voice_notes = RecordingProductivity(None)
        insights = RecordingProductivity("Summarized “Launch sync”.")
        notes = RecordingProductivity("wrong controller")
        commands = VoiceCommandRouter(
            replies,
            ModelRegistry([]),
            notes=notes,
            voice_notes=voice_notes,
            note_insights=insights,
        )

        text = "summarize the last note"
        assert await commands(text) == "Summarized “Launch sync”."
        # The recorder saw it first and declined; text notes never did.
        assert voice_notes.seen == [text]
        assert insights.seen == [text]
        assert notes.seen == []

    async def test_full_glasses_quicknote_grammar_drives_the_real_controllers(
        self, tmp_path: Path
    ) -> None:
        """The exact wearer phrases, through the REAL production controllers:
        record → save (transcribed) → summarize → ask. This is the routing
        half of the glasses chain; the orchestrator wake-strip half is pinned
        in test_orchestrator."""

        class Frames(AudioFrameSource):
            def __init__(self) -> None:
                self.callback: AudioFrameCallback | None = None

            def on_audio_frame(self, callback: AudioFrameCallback) -> None:
                self.callback = callback

            def emit(self, seq: int) -> None:
                assert self.callback is not None
                self.callback(AudioFrame(
                    # Speech-level audio: live transcription (FEAT-002) never
                    # sends near-silence to STT.
                    pcm=b"\xb8\x0b\x48\xf4" * 20,
                    connection_generation="socket-a",
                    seq=seq,
                    source="glasses",
                    speaker="wearer",
                    received_at_us=100 + seq,
                ))

        class Stt(SttEngine):
            async def feed(self, pcm: bytes) -> Transcript | None:
                del pcm
                return None

            async def flush(self) -> Transcript | None:
                return Transcript("QA owns the final pass. Ship Friday.", True)

            def reset(self) -> None: ...

        class Completer:
            async def complete(
                self, *, system: str, text: str, max_tokens: int = 700
            ) -> str | None:
                del max_tokens
                if "distill" in system:
                    assert "Ship Friday" in text
                    return (
                        "SUMMARY:\nQA owns the final pass and the ship date is "
                        "Friday.\nKEY POINTS:\n- Ship Friday\nTODOS:\n- none\n"
                    )
                return "QA owns the final pass."

        durable = DurableStore(tmp_path / "joat.sqlite3")
        notes_store = VoiceNoteStore(durable)
        frames = Frames()
        capture = VoiceNoteCaptureController(
            notes_store,
            AudioRouter(frames),
            Stt(),
            clock_us=iter(range(1_000, 2_000)).__next__,
            id_factory=lambda: "capture-1",
        )
        insights = NoteInsightsService(
            notes_store, Completer(), clock_us=lambda: 5_000,
        )
        commands = VoiceCommandRouter(
            RecordingReplies(),
            ModelRegistry([]),
            voice_notes=capture,
            note_insights=NoteInsightCommandController(insights),
        )

        with durable:
            started = await commands("start a quicknote called Standup")
            assert started is not None and started.startswith(
                "Recording Voice Note: Standup"
            )
            frames.emit(0)
            saved = await commands("save quicknote")
            assert saved is not None and saved.startswith("Saved Voice Note")

            summarized = await commands("summarize the last note")
            assert summarized is not None and summarized.startswith(
                "Summarized “Standup”"
            )
            answered = await commands("ask the last note who owns the final pass")
            assert answered == "QA owns the final pass."


class RecordingOpens:
    """Duck-typed CapabilityOpenCommandController."""

    def __init__(self, result: str | None = None) -> None:
        self.seen: list[str] = []
        self.reflected = 0
        self._result = result

    async def handle_command(self, text: str) -> str | None:
        self.seen.append(text)
        return self._result

    async def reflect_quicknotes(self) -> None:
        self.reflected += 1


class TestCapabilityOpenRouting:
    async def test_open_claims_before_the_general_reply_engine(self) -> None:
        replies = RecordingReplies()
        opens = RecordingOpens("Opening Messages.")
        commands = VoiceCommandRouter(
            replies, ModelRegistry([]), capability_opens=opens,
        )
        assert await commands("show my messages") == "Opening Messages."
        assert opens.seen == ["show my messages"]

    async def test_capture_commands_repaint_the_quicknotes_surface(self) -> None:
        replies = RecordingReplies()
        opens = RecordingOpens(None)
        voice_notes = RecordingProductivity("Recording paused.")
        commands = VoiceCommandRouter(
            replies, ModelRegistry([]),
            voice_notes=voice_notes, capability_opens=opens,
        )
        assert await commands("pause recording") == "Recording paused."
        assert opens.reflected == 1

    async def test_unmatched_speech_never_reflects(self) -> None:
        replies = RecordingReplies()
        opens = RecordingOpens(None)
        commands = VoiceCommandRouter(
            replies, ModelRegistry([]), capability_opens=opens,
        )
        assert await commands("what is the weather") is None
        assert opens.reflected == 0
