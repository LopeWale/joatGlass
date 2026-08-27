"""A round table: N collaborators, one speaking at a time, in an order the
wearer controls.

The wearer's model of a debate, in their words: colleagues sitting in a
circle, discussing, "so the final solution excludes gaps due to the
different perspective" — peer review, not a contest. And explicitly: "the
debate is going to be one at a time, and I wanna see it streamed ... because
then I cannot prove that that was debated. I wanna see."

Two properties follow, and both are structural rather than stylistic:

SEQUENTIAL BY CONSTRUCTION. Each speaker's prompt contains the transcript so
far, so turn N+1 literally cannot be produced before turn N exists. Nothing
enforces "one at a time" — it is the only thing the data allows.

DIRECTABLE. The wearer picks who speaks next ("codex first", "claude,
respond", "openclaw, what do you think"). Left alone it round-robins so a
debate never stalls waiting for direction.

Participants are anything with `stream(prompt) -> AsyncIterator[str]`, which
is what makes a third voice cost a file rather than a redesign: the codex and
claude CLIs already satisfy it, and so does the OpenClaw gateway.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass

log = logging.getLogger(__name__)

Streamer = Callable[[str], AsyncIterator[str]]


FAILURE_MARK = "[!]"
"""Opening a turn with this means "this seat could not speak".

Observed live: `claude -p` exits 1 and prints "You're out of usage credits"
ON STDOUT, so a collaborator's failure looks exactly like a short opinion.
Recorded as a position it becomes something the next speaker argues against
and the judge scores — a debate about an error message. The wearer must
still SEE the failure, so it is a visible turn that is not a quotable one.
"""


def failure_notice(name: str, detail: str) -> str:
    return f"{FAILURE_MARK} {name} could not answer: {detail}".strip()


def is_failure(text: str) -> bool:
    """True if ANY line marks a failure — the notice usually comes last.

    A CLI streams what it has and only then dies, so the live shape is prose
    followed by the mark. A truncated half-answer with an error stapled to it
    is not a position anyone should be asked to rebut, so the whole turn is
    disqualified rather than salvaged.
    """
    return any(line.lstrip().startswith(FAILURE_MARK)
               for line in text.splitlines())


@dataclass(frozen=True)
class Participant:
    """One seat at the table."""

    name: str
    """Short, lowercase, what the wearer says to address them: 'codex'."""
    role: str
    """What this seat is FOR — shown on the transcript and in the prompt."""
    stream: Streamer

    @property
    def label(self) -> str:
        return f"{self.name.upper()} ({self.role})"


class RoundTable:
    """Turn order for a set of participants, directable by name."""

    def __init__(self, participants: Sequence[Participant]) -> None:
        if not participants:
            raise ValueError("a round table needs at least one participant")
        self._participants = list(participants)
        self._index = 0

    def __len__(self) -> int:
        return len(self._participants)

    @property
    def names(self) -> list[str]:
        return [p.name for p in self._participants]

    def find(self, name: str) -> Participant | None:
        """Resolve a spoken name to a seat, tolerantly.

        STT mangles names, and the wearer says "ask codex" rather than
        "codex". So match on containment in either direction: a transcript
        containing the name, or a name containing the (truncated) word.
        """
        said = name.strip().lower()
        if not said:
            return None
        for p in self._participants:
            if p.name == said:
                return p
        for p in self._participants:
            if p.name in said or (len(said) >= 4 and said in p.name):
                return p
        return None

    def direct(self, name: str) -> Participant | None:
        """Make `name` the next speaker. Returns the seat, or None if unknown."""
        seat = self.find(name)
        if seat is None:
            return None
        self._index = self._participants.index(seat)
        return seat

    def next_speaker(self) -> Participant:
        """The seat whose turn it is; advances the rotation."""
        seat = self._participants[self._index % len(self._participants)]
        self._index = (self._index + 1) % len(self._participants)
        return seat

    def peek(self) -> Participant:
        return self._participants[self._index % len(self._participants)]


def parse_direction(text: str, table: RoundTable) -> Participant | None:
    """A spoken turn-direction, or None if the wearer said something else.

    Deliberately narrow: only phrasings that clearly nominate a speaker
    count, so ordinary discussion mentioning a participant's name is not
    mistaken for a command. "What did codex mean?" must not re-order the
    table.
    """
    # Normalize punctuation to spaces: people address a colleague with a
    # comma ("codex, your turn"), and STT sprinkles its own. Matching raw
    # text would recognise "codex your turn" and miss the way anyone
    # actually says it.
    said = " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())
    if not said:
        return None
    for seat in table._participants:  # noqa: SLF001 - same module, one owner
        n = seat.name
        for phrasing in (
            f"{n} respond", f"{n} go", f"{n} first", f"{n} next",
            f"{n} your turn", f"ask {n}", f"{n} what do you think",
            f"let {n}", f"over to {n}", f"{n} please",
        ):
            if said.startswith(phrasing) or said == n:
                return seat
    return None


def build_prompt(topic: str, speaker: Participant, transcript: Sequence[tuple[str, str]],
                 contract: str = "") -> str:
    """What this speaker sees: the question, the room, and their job.

    The transcript IS the sequencing mechanism — a speaker cannot be prompted
    before the turns they are answering exist.
    """
    lines = [f"Question under discussion: {topic}", ""]
    if transcript:
        lines.append("What has been said so far, in order:")
        for label, text in transcript:
            lines.append(f"\n[{label}]\n{text.strip()}")
        lines.append("")
        lines.append(
            f"You are {speaker.label}. Add what the others missed, name any "
            "point you think is wrong and why, and say plainly where you "
            "agree — do not restate what has already been said."
        )
    else:
        lines.append(
            f"You are {speaker.label}. Open the discussion with a concrete "
            "position."
        )
    lines.append("Under 120 words, plain text, no markdown.")
    return "\n".join(lines) + contract
