"""Which model each seat runs, changeable while the assistant is running.

Every model was previously fixed at launch: `--llm-model`, `--stt-model`, and
`CODEX_MODEL` were read once at boot, so trying a different critic meant
killing the engine. From the glasses that is not a setting at all.

Two things make this switchable without a wire-protocol change:

CHOICES ARE NAMED THE WAY THEY ARE SPOKEN. The wearer says "sonnet", never
"claude-sonnet-4-5-20250929", and whisper would mangle the full id anyway.
Each option carries a short spoken label plus the id actually passed to the
tool, so the voice grammar matches on the label.

A SLOT IS READ AT CALL TIME, NOT AT BOOT. Agents ask the registry for their
model when they are about to run, which is what makes a change take effect on
the next turn rather than the next launch.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

Applier = Callable[[str | None], Awaitable[bool]]

DEFAULT_CODEX_MODEL = "gpt-5.6-terra"
"""Verified 2026-08-27: terra answers, and spark is refusing with "You've hit
your usage limit … try again at Aug 31st". Both stay selectable — which is
the point of the slot — but the DEFAULT has to be a seat that can speak, or
the proposer is silent at boot for a reason no log explains."""

DEFAULT_STT_MODEL = "large-v3_turbo"

DEFAULT_ASSISTANT_SEAT = "claude"
"""Which subscription answers the wearer (agents/cli_reply_engine.py):
Claude Code on the Max plan by default, Codex on the ChatGPT plan, or the
resident MLX model. Never an API key (RULES.md #9, #10)."""


@dataclass(frozen=True)
class ModelOption:
    """One choice in a slot."""

    label: str
    """What the wearer says and the HUD shows: 'sonnet', 'turbo'."""
    id: str | None
    """What the tool is actually given. None = pass no flag and let the tool
    keep its own default, which is how the current behaviour is preserved as
    an explicit, selectable choice rather than an unnamed fallback."""
    note: str = ""

    @property
    def display(self) -> str:
        return f"{self.label} — {self.note}" if self.note else self.label


@dataclass
class ModelSlot:
    """One switchable model, and how to make a change take effect."""

    key: str
    """Addressed by voice: 'codex', 'claude', 'stt'."""
    title: str
    options: tuple[ModelOption, ...]
    aliases: tuple[str, ...] = ()
    """Other words the wearer might use for this slot ('critic', 'speech')."""
    index: int = 0
    apply: Applier | None = None
    """Slots backed by a resident process need work to switch; a slot that is
    only an argv field needs none."""

    @property
    def current(self) -> ModelOption:
        return self.options[self.index]

    def find(self, said: str) -> int | None:
        want = said.strip().lower()
        for i, opt in enumerate(self.options):
            if opt.label.lower() == want:
                return i
        for i, opt in enumerate(self.options):
            if want and (want in opt.label.lower() or want == (opt.id or "").lower()):
                return i
        return None


class ModelRegistry:
    """The switchable models, and the one place that knows the current pick."""

    def __init__(self, slots: Sequence[ModelSlot]) -> None:
        self._slots = list(slots)

    @property
    def slots(self) -> list[ModelSlot]:
        return list(self._slots)

    def slot(self, key: str) -> ModelSlot | None:
        """A slot by any name the wearer could reasonably have heard.

        Including its TITLE. `summary()` answers "which models are you
        using" with "Codex seat: terra · Claude seat: default", so those
        are the words the wearer has just been taught — and "switch the
        claude seat to sonnet" was rejected, because only the key and the
        aliases were matched. A surface that teaches one vocabulary and
        accepts another sends the wearer away thinking the feature is
        broken.
        """
        want = key.strip().lower()
        for s in self._slots:
            if s.key == want or want in s.aliases or s.title.lower() == want:
                return s
        return None

    def current(self, key: str) -> str | None:
        """What this seat should run RIGHT NOW.

        Agents call this per turn. Returning None means "pass no model flag",
        which is a real choice, not a missing one.
        """
        slot = self.slot(key)
        return slot.current.id if slot else None

    def provider(self, key: str) -> Callable[[], str | None]:
        """A late-bound reader, for agents constructed once at boot."""
        return lambda: self.current(key)

    async def select(self, key: str, said: str) -> str:
        """Apply a choice. Returns the line to show the wearer.

        A failed switch ROLLS BACK. Whisper is the reason: an unknown model
        name leaves serve unable to start, and a wearer whose glasses have
        silently gone deaf has no way to ask for the old one back.
        """
        slot = self.slot(key)
        if slot is None:
            return f"No model called {key}."
        index = slot.find(said)
        if index is None:
            return f"{slot.title}: no option called {said}. Try {self.choices(slot)}."
        if index == slot.index:
            return f"{slot.title} already on {slot.current.label}."
        previous, slot.index = slot.index, index
        if slot.apply is None:
            log.info("%s -> %s (next turn)", slot.key, slot.current.label)
            return f"{slot.title} now {slot.current.label}."
        ok = await slot.apply(slot.current.id)
        if not ok:
            slot.index = previous
            log.warning("%s could not switch to %s — rolled back to %s",
                        slot.key, slot.options[index].label, slot.current.label)
            return (f"{slot.title} could not load {slot.options[index].label} — "
                    f"still on {slot.current.label}.")
        log.info("%s -> %s (applied)", slot.key, slot.current.label)
        return f"{slot.title} now {slot.current.label}."

    def choices(self, slot: ModelSlot) -> str:
        return ", ".join(o.label for o in slot.options)

    def summary(self) -> str:
        return " · ".join(f"{s.title}: {s.current.label}" for s in self._slots)

    def rows(self, key: str | None = None) -> list[str]:
        """HUD list rows — every slot, or the options within one.

        Marked with '*' the way the resume-policy menu marks its active row,
        so the two settings surfaces read the same on glass.
        """
        if key is None:
            return [f"{s.title}: {s.current.label}" for s in self._slots]
        slot = self.slot(key)
        if slot is None:
            return []
        return [f"{'*' if i == slot.index else ' '} {o.display}"
                for i, o in enumerate(slot.options)]


# ── spoken commands ─────────────────────────────────────────────────────────
#
# Deliberately narrow, for the same reason turn-direction is: a config change
# triggered by ordinary conversation is worse than one that needs rephrasing.

_USE = re.compile(
    r"^(?:use|switch to|run|set) (?P<model>[a-z0-9._\- ]+?)"
    r"(?: for(?: the)? (?P<slot>[a-z0-9\- ]+))?$"
)
_SWITCH = re.compile(
    r"^(?:switch|set|change) (?:the )?(?P<slot>[a-z0-9\- ]+?) to (?P<model>[a-z0-9._\- ]+)$"
)
_REPORT = re.compile(
    r"^(?:which|what) models?(?: are you| do you)?(?: using| on| running)?$"
)


async def handle_model_command(text: str, registry: ModelRegistry) -> str | None:
    """A spoken model change, or None when the wearer said something else.

    Voice is the glasses' primary input, so this needs no menu row, no new
    wire verb, and no plugin change to work today.
    """
    said = " ".join(re.sub(r"[^a-z0-9._\- ]+", " ", text.lower()).split())
    if not said:
        return None
    if _REPORT.match(said):
        return registry.summary()
    for pattern in (_SWITCH, _USE):
        m = pattern.match(said)
        if not m:
            continue
        model = (m.group("model") or "").strip()
        spoken_slot = (m.groupdict().get("slot") or "").strip()
        if spoken_slot:
            slot = registry.slot(spoken_slot)
            if slot is None:
                return None  # not about models at all — let the assistant answer
            return await registry.select(slot.key, model)
        # No slot named: only act when exactly one slot offers that option,
        # so "use sonnet" works while an ambiguous word stays a question.
        matches = [s for s in registry.slots if s.find(model) is not None]
        if len(matches) == 1:
            return await registry.select(matches[0].key, model)
        if len(matches) > 1:
            return (f"{model} is offered by "
                    + " and ".join(s.title for s in matches)
                    + " — say which one.")
        return None
    return None


# ── defaults ────────────────────────────────────────────────────────────────

def assistant_slot(default: str = DEFAULT_ASSISTANT_SEAT) -> ModelSlot:
    """The assistant's seat, named the way it is spoken.

    Kept apart from ``default_slots`` so the Brainstorm seats and the speech
    slot keep their exact shape; ``__main__`` appends this one. The seat
    switch reads it per turn, so "use codex for the assistant" lands on the
    next question without a restart. Which MODEL each seat runs is the
    existing Claude seat / Codex seat slot.
    """
    options = (
        ModelOption("claude", "claude", "Claude Code on the Max subscription; streams"),
        ModelOption("codex", "codex", "Codex CLI on the ChatGPT subscription"),
        ModelOption("local", "local", "the resident MLX model; offline"),
    )
    index = next((i for i, o in enumerate(options) if o.id == default), 0)
    return ModelSlot(
        key="assistant", title="Assistant", aliases=("agent", "mind", "joat", "seat"),
        options=options, index=index,
    )


def default_slots(
    codex_default: str = DEFAULT_CODEX_MODEL,
    stt_default: str = DEFAULT_STT_MODEL,
    stt_apply: Applier | None = None,
) -> list[ModelSlot]:
    """The seats worth switching, named as they are spoken.

    Codex and Claude cost nothing to switch — the model is an argv field on
    the next subprocess call. Whisper is a resident process, so its slot
    carries an applier that respawns it.
    """
    codex = [
        ModelOption("terra", "gpt-5.6-terra", "default"),
        ModelOption("spark", "gpt-5.3-codex-spark", "faster; limit resets Aug 31"),
    ]
    if codex_default not in [o.id for o in codex]:
        codex.insert(0, ModelOption(codex_default.split("-")[-1], codex_default))
    stt = [
        ModelOption("turbo", "large-v3_turbo", "default, ~1s"),
        ModelOption("large", "large-v3", "most accurate; downloads on first use"),
        ModelOption("small", "small", "fastest; downloads on first use"),
        ModelOption("base", "base", "smallest"),
    ]
    return [
        ModelSlot(
            key="codex", title="Codex seat", aliases=("proposer",),
            options=tuple(codex),
            index=next(i for i, o in enumerate(codex) if o.id == codex_default),
        ),
        ModelSlot(
            key="claude", title="Claude seat", aliases=("critic",),
            options=(
                # None keeps the CLI's own default — today's behaviour, made
                # an explicit choice instead of an unnamed fallback.
                ModelOption("default", None, "whatever the CLI picks"),
                # Only aliases this CLI's own --help documents. An alias it
                # rejects is worse than a missing option: a debate seat has
                # no applier to roll back, so a bad name is not caught until
                # the seat silently fails mid-debate.
                ModelOption("sonnet", "sonnet", "balanced"),
                ModelOption("opus", "opus", "strongest"),
                ModelOption("fable", "fable", "newest"),
            ),
        ),
        ModelSlot(
            key="stt", title="Speech", aliases=("speech", "whisper", "transcription",
                                                "hearing"),
            options=tuple(stt), apply=stt_apply,
            index=next((i for i, o in enumerate(stt) if o.id == stt_default), 0),
        ),
    ]
