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
from typing import Literal
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
        self._pending: list[Participant] = []

    def __len__(self) -> int:
        return len(self._participants)

    @property
    def names(self) -> list[str]:
        return [p.name for p in self._participants]

    @property
    def seats(self) -> list[Participant]:
        """Everyone at the table, in their seated order.

        Distinct from the SPEAKING order, which a wearer redirect can
        reshuffle. Used to launch round 1's independent turns together.
        """
        return list(self._participants)

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

    def start_round(self) -> None:
        """Snapshot who owes a turn: everyone, exactly once.

        "A round contains one turn from every seated collaborator" is the
        wearer's invariant, and before this snapshot existed it was only a
        default: `direct()` moved the rotation cursor, so a mid-round
        redirect made one seat speak twice and another zero times in that
        round. Now a redirect can only REORDER the seats still owing a
        turn — the count is structural.
        """
        ordered = self._participants[self._index:] + self._participants[:self._index]
        self._pending = list(ordered)

    def direct(self, name: str) -> Participant | None:
        """Give `name` the floor. Returns the seat, or None if unknown.

        Mid-round, a seat that still owes a turn jumps to the front of the
        pending order. A seat that has ALREADY spoken this round is not
        given a second turn — it opens the next round instead, which is
        what the wearer meant a beat too late.
        """
        seat = self.find(name)
        if seat is None:
            return None
        if seat in self._pending:
            self._pending.remove(seat)
            self._pending.insert(0, seat)
        else:
            self._index = self._participants.index(seat)
        return seat

    def next_speaker(self) -> Participant:
        """The seat whose turn it is; consumes it from the current round."""
        if not self._pending:
            self.start_round()
        seat = self._pending.pop(0)
        # The cursor trails the speaker so the NEXT round opens with the
        # seat after the last one heard — rotation survives the snapshot.
        self._index = (self._participants.index(seat) + 1) % len(self._participants)
        return seat

    def peek(self) -> Participant:
        if self._pending:
            return self._pending[0]
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
            # "your turn" was recognised and "you next" was not, which is
            # the kind of gap that makes a voice feature feel broken rather
            # than limited: the wearer says a natural variant, nothing
            # happens, and there is no way to tell why. Still narrow —
            # every phrasing here nominates a speaker and cannot be an
            # ordinary remark about one.
            f"{n} you next", f"{n} you re next", f"{n} youre next",
            f"{n} take it", f"{n} take this", f"{n} answer",
            f"{n} have a go", f"{n} weigh in", f"{n} thoughts",
            f"back to {n}", f"now {n}",
        ):
            if said.startswith(phrasing) or said == n:
                return seat
    return None


RelayMode = Literal["open", "cross", "critique", "build"]
"""How the wearer routed this turn.

The wearer's own framing: send the answer to the other model "to debate",
or have them pass it back and forth. Those are different instructions on
identical text, and the difference is the whole distinction between a
debate and a brainstorm — critique hunts the weakest claim, build forbids
disputing and demands an addition. 'cross' is the default blend when the
wearer has not steered.
"""

_MODE_INSTRUCTION: dict[str, str] = {
    "cross": (
        "Add what the others missed, name any point you think is wrong and "
        "why, and say plainly where you agree — do not restate what has "
        "already been said."
    ),
    "critique": (
        "Find the WEAKEST claim above and say plainly why it fails. Quote "
        "the claim you are attacking. Do not add new proposals and do not "
        "soften it with agreement — this turn exists to stress-test."
    ),
    "build": (
        "Take the STRONGEST idea above and extend it. Do not dispute it, do "
        "not restate it, and do not hedge — say what it makes possible next, "
        "or what it needs to work."
    ),
}


#: The two instructions that make the same text a debate or a
#: brainstorm. Audited against phrasings people reach for rather than the
#: ones that happened to be listed first: "disagree with that", "add to
#: that" and "extend that" were all silently unrecognised, and an
#: unrecognised routing instruction does not fail — it becomes an ordinary
#: cross-review turn, so the wearer sees a plausible answer to a question
#: they did not ask.
_CRITIQUE = re.compile(
    r"\b(tear (?:it|that) apart|pull (?:it|that) apart|critique (?:it|that|this)|"
    r"poke holes|find the (?:weak|weakest)|what.s wrong with (?:it|that)|"
    r"push back(?: on (?:it|that))?|challenge (?:it|that)|"
    r"disagree with (?:it|that)|argue against (?:it|that)|"
    r"attack (?:it|that)|stress test (?:it|that)|"
    r"where does (?:it|that) break)\b"
)
_BUILD = re.compile(
    r"\b(build on (?:it|that|this)|expand on (?:it|that|this)|"
    r"run with (?:it|that)|take (?:it|that) further|"
    r"develop (?:it|that)|go deeper(?: on (?:it|that))?|"
    r"add to (?:it|that)|extend (?:it|that)|"
    r"keep going with (?:it|that)|flesh (?:it|that) out)\b"
)


def parse_relay_mode(text: str) -> RelayMode | None:
    """How the wearer wants this turn routed, or None if they didn't say.

    Recognised alongside a nomination, so "claude, tear that apart" both
    hands over the floor and sets the instruction — which is how a person
    actually says it, rather than two separate commands.
    """
    said = " ".join(re.sub(r"[^a-z0-9' ]+", " ", text.lower()).split())
    if _CRITIQUE.search(said):
        return "critique"
    if _BUILD.search(said):
        return "build"
    return None


def build_prompt(topic: str, speaker: Participant, transcript: Sequence[tuple[str, str]],
                 contract: str = "", mode: RelayMode = "cross") -> str:
    """What this speaker sees: the question, the room, and their job.

    The transcript IS the sequencing mechanism — a speaker cannot be prompted
    before the turns they are answering exist. ``mode`` is how the wearer
    routed the turn; identical text with opposite instructions.
    """
    lines = [f"Question under discussion: {topic}", ""]
    if transcript:
        lines.append("What has been said so far, in order:")
        for label, text in transcript:
            lines.append(f"\n[{label}]\n{text.strip()}")
        lines.append("")
        lines.append(
            f"You are {speaker.label}. "
            + _MODE_INSTRUCTION.get(mode, _MODE_INSTRUCTION["cross"])
        )
    else:
        lines.append(
            f"You are {speaker.label}. Open the discussion with a concrete "
            "position."
        )
    # The production image HUD has no Brainstorm scrollback: it exposes a
    # bounded live activity window and reserves swipe-down for cancellation.
    # Keep every generated turn concise enough to remain useful at a glance;
    # the terminal is the companion surface for a longer discussion.
    lines.append(
        "Open with one line, exactly 'GIST: <your position in a dozen "
        "words>', then give one concise response. Under 120 words in total, "
        "plain text, no markdown."
    )
    return "\n".join(lines) + contract
