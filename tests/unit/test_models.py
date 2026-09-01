"""Switching models while the assistant is running.

Every model used to be a launch flag, so "change the model" meant "restart
the engine" — not a setting at all from the glasses. These pin the two
properties that make it a setting: a slot is read per turn, and a switch
that fails leaves the wearer on something that still works.
"""

from __future__ import annotations

import pytest

from joat.services.models import (
    ModelOption, ModelRegistry, ModelSlot, default_slots, handle_model_command,
)


def registry(stt_apply=None) -> ModelRegistry:
    return ModelRegistry(default_slots(stt_apply=stt_apply))


class TestSlotsAreReadPerTurn:
    async def test_a_switch_lands_on_the_next_turn_not_the_next_launch(self) -> None:
        reg = registry()
        read = reg.provider("codex")          # what an agent holds from boot
        assert read() == "gpt-5.6-terra"
        await reg.select("codex", "spark")
        assert read() == "gpt-5.3-codex-spark"  # same provider, new answer

    async def test_default_means_pass_no_flag(self) -> None:
        # Today's behaviour — the claude CLI on its own default — stays
        # reachable as an explicit choice rather than an unnamed fallback.
        reg = registry()
        assert reg.current("claude") is None
        await reg.select("claude", "sonnet")
        assert reg.current("claude") == "sonnet"
        await reg.select("claude", "default")
        assert reg.current("claude") is None


class TestFailedSwitchRollsBack:
    async def test_a_model_that_will_not_load_leaves_the_old_one_running(self) -> None:
        """Whisper is why this matters: an unknown model name leaves serve
        unable to start, and a wearer whose glasses have gone deaf has no
        way to ask for the old model back."""
        applied: list[str | None] = []

        async def refuse(model: str | None) -> bool:
            applied.append(model)
            return False

        reg = registry(stt_apply=refuse)
        said = await reg.select("stt", "large")
        assert applied == ["large-v3"]
        assert reg.current("stt") == "large-v3_turbo"   # rolled back
        assert "still on turbo" in said

    async def test_a_successful_switch_sticks(self) -> None:
        async def accept(model: str | None) -> bool:
            return True

        reg = registry(stt_apply=accept)
        said = await reg.select("stt", "large")
        assert reg.current("stt") == "large-v3"
        assert said == "Speech now large."


class TestSpokenCommands:
    async def test_names_the_seat(self) -> None:
        reg = registry()
        assert await handle_model_command("use sonnet for the critic", reg) is not None
        assert reg.current("claude") == "sonnet"

    async def test_switch_phrasing(self) -> None:
        reg = registry()
        await handle_model_command("switch codex to spark", reg)
        assert reg.current("codex") == "gpt-5.3-codex-spark"

    async def test_an_unambiguous_model_needs_no_seat(self) -> None:
        reg = registry()
        await handle_model_command("use opus", reg)
        assert reg.current("claude") == "opus"

    async def test_reports_what_is_running(self) -> None:
        reg = registry()
        said = await handle_model_command("what models are you using", reg)
        assert said is not None
        assert "Codex seat: terra" in said and "Speech: turbo" in said

    @pytest.mark.parametrize("utterance", [
        "what is the weather",
        "use the side door when you arrive",   # 'use ...' but no known model
        "switch the lights to blue",
        "tell me about opus the composer",     # a model NAME, but not a command
    ])
    async def test_ordinary_speech_is_left_to_the_assistant(self, utterance: str) -> None:
        # A config change triggered by conversation is worse than one that
        # needs rephrasing, so anything unrecognised returns None and falls
        # through to the reply engine.
        reg = registry()
        assert await handle_model_command(utterance, reg) is None
        assert reg.current("claude") is None  # nothing moved

    async def test_an_unknown_option_says_what_is_on_offer(self) -> None:
        reg = registry()
        said = await handle_model_command("use gpt-9 for the critic", reg)
        assert said is not None and "sonnet" in said
        assert reg.current("claude") is None


class TestHudRows:
    def test_slot_rows_mark_the_active_choice(self) -> None:
        reg = registry()
        rows = reg.rows("claude")
        assert rows[0].startswith("*")            # 'default' is active
        assert all(not r.startswith("*") for r in rows[1:])

    def test_overview_rows_name_every_slot(self) -> None:
        rows = registry().rows()
        assert rows == ["Codex seat: terra", "Claude seat: default", "Speech: turbo"]


class TestTheNamesItReportsAreNamesItAccepts:
    """`summary()` answers "which models are you using" with
    "Codex seat: terra · Claude seat: default · Speech: turbo".

    Those are the words the wearer has just been taught, and until now
    "switch the claude seat to sonnet" was rejected — only the key and
    aliases matched. A surface that teaches one vocabulary and accepts
    another sends the wearer away believing the feature does not work,
    which is the same defect as a HUD hint describing an action the
    gesture does not perform.
    """

    async def test_every_reported_slot_title_is_speakable(self) -> None:
        from joat.services.models import (
            ModelRegistry, default_slots, handle_model_command,
        )

        registry = ModelRegistry(default_slots())
        for slot in registry.slots:
            assert registry.slot(slot.title) is slot, slot.title
            assert registry.slot(slot.title.upper()) is slot, slot.title

        # And through the spoken grammar, the way it is actually said.
        assert await handle_model_command(
            "switch the claude seat to sonnet", registry
        ) is not None
        assert await handle_model_command(
            "use sonnet for the codex seat", registry
        ) is not None

    async def test_ordinary_speech_is_still_not_a_config_change(self) -> None:
        from joat.services.models import (
            ModelRegistry, default_slots, handle_model_command,
        )

        registry = ModelRegistry(default_slots())
        for said in ("use the calendar to check tomorrow",
                     "switch the lights off",
                     "set a reminder for six",
                     "switch the seat to the window"):
            assert await handle_model_command(said, registry) is None, said
