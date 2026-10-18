"""Spoken commitments: the things the wearer said they will do.

A summary's NEXT STEPS are written by the local model and are often its own
invention ("Schedule a meeting with the IT department" from a note that never
said so). A reminder must be something the wearer actually said. So this is a
second, narrow pass: the model proposes tasks WITH the exact words they came
from, and plain code keeps only what the transcript backs up.

Kept only when all of these hold (checked here, never trusted to the model):

- the quoted words are in the transcript, word for word;
- the quote carries a commitment cue ("I need to", "I'll", "remind me", ...);
- the task's own words are mostly in that quote;
- one task per sentence, and a task said twice is one task.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)


class _Completer(Protocol):
    async def complete(
        self, *, system: str, text: str, max_tokens: int = 700
    ) -> str | None: ...


#: Same ceiling the summary uses for one model pass over a transcript.
_CHUNK_CHARS = 20_000
_MAX_CHUNKS = 8
MAX_COMMITMENTS = 8
_MIN_QUOTE_WORDS = 3
_TASK_WORDS_IN_QUOTE = 0.6
_NEAR_DUPLICATE = 0.6
_MAX_TASK_CHARS = 160

COMMITMENT_SYSTEM = (
    "You read the transcript of a recorded note and list the tasks the speaker "
    "said THEY will do, need to do, must do, or asked to be reminded of.\n"
    "Reply with one block per task, in exactly this shape and nothing else:\n"
    "TASK: the task as a short instruction of 3 to 12 words\n"
    "QUOTE: the speaker's own words, copied exactly from the transcript\n"
    "Never invent a task. Do not list opinions, facts, ideas, or things other "
    "people will do. One task for each sentence. If the speaker named no task "
    "of their own, reply with the single word NONE."
)

_CUE = re.compile(
    r"\b(?:i need to|i will|i'll|i have to|i must|i'm going to|i am going to|"
    r"i'm gonna|i've got to|i have got to|i gotta|i've gotta|remind me|don't forget|"
    r"do not forget|we need to)\b"
)
_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_DAY = re.compile(
    r"\b(today|tonight|tomorrow|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday)\b"
)
_FILLER = frozenset({
    "a", "an", "the", "to", "of", "and", "for", "my", "on", "in", "at", "with",
    "up", "it", "i", "we", "me", "our", "that", "this", "so", "then", "also",
})
_HEADER = re.compile(r"^[\s>*_#\-•\d.)]*(task|quote)\s*[*_]*\s*:\s*[*_]*\s*(.*)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Commitment:
    """One task the wearer said they would do."""

    #: The reminder's title: a short instruction.
    task: str
    #: The wearer's own sentence it came from, exactly as transcribed.
    quote: str
    #: A day word in the quote ("tomorrow", "friday"), if the wearer named one.
    day: str | None = None

    @property
    def key(self) -> str:
        """Identity of the sentence, stable across summaries and restarts."""
        return hashlib.sha256(" ".join(_words(self.quote)).encode("utf-8")).hexdigest()[:20]


def _words(text: str) -> list[str]:
    return _WORD.findall(text.casefold().replace("’", "'").replace("‘", "'"))


def _stem(word: str) -> str:
    return word[:5] if len(word) > 5 else word


def _content(text: str) -> set[str]:
    return {_stem(word) for word in _words(text) if word not in _FILLER}


def parse_blocks(raw: str) -> list[tuple[str, str]]:
    """TASK/QUOTE blocks in the order the model wrote them; tolerant of decoration."""
    pairs: list[tuple[str, str]] = []
    task: str | None = None
    quote: str | None = None
    current: str | None = None

    def flush() -> None:
        nonlocal task, quote, current
        if task and quote:
            pairs.append((task, quote))
        task, quote, current = None, None, None

    for line in raw.splitlines():
        match = _HEADER.match(line)
        if match is not None:
            field = match.group(1).casefold()
            value = match.group(2).strip().strip("\"“”").strip()
            if field == "task":
                flush()
                task, current = value, "task"
            else:
                quote, current = value, "quote"
            continue
        text = line.strip()
        if not text:
            continue
        if current == "quote" and quote is not None:
            quote = f"{quote} {text}"
        elif current == "task" and task is not None:
            task = f"{task} {text}"
    flush()
    return pairs


def spoken_day(quote: str) -> str | None:
    """The first day word the wearer said in ``quote``, or None."""
    match = _DAY.search(quote.casefold())
    return None if match is None else ("today" if match.group(1) == "tonight" else match.group(1))


def _title(task: str) -> str:
    text = " ".join(task.split()).strip(" \"“”'.,;:!-–—")
    text = re.sub(r"^(?:to|please)\s+", "", text, flags=re.IGNORECASE)
    if len(text) > _MAX_TASK_CHARS:
        head = text[:_MAX_TASK_CHARS].rsplit(" ", 1)[0]
        text = head or text[:_MAX_TASK_CHARS]
    return text[:1].upper() + text[1:]


def _cue_sentence(quote: str) -> str | None:
    """The one sentence of ``quote`` that holds a commitment cue."""
    for sentence in _SENTENCE_END.split(" ".join(quote.split())):
        if _CUE.search(" ".join(_words(sentence))):
            return sentence.strip()
    return None


def keep_backed_commitments(
    proposed: list[tuple[str, str]], transcript: str,
) -> tuple[Commitment, ...]:
    """Keep only the proposals the transcript backs up (see the module notes)."""
    haystack = f" {' '.join(_words(transcript))} "
    kept: list[Commitment] = []
    seen_quotes: set[str] = set()
    for task_text, quote_text in proposed:
        sentence = _cue_sentence(quote_text)
        if sentence is None:
            continue
        quote_words = _words(sentence)
        if len(quote_words) < _MIN_QUOTE_WORDS:
            continue
        if f" {' '.join(quote_words)} " not in haystack:
            continue
        task = _title(task_text)
        task_stems = _content(task)
        if not task_stems:
            continue
        in_quote = _content(sentence)
        if len(task_stems & in_quote) / len(task_stems) < _TASK_WORDS_IN_QUOTE:
            continue
        normalized_quote = " ".join(quote_words)
        if normalized_quote in seen_quotes:
            continue
        if any(
            len(task_stems & _content(other.task)) / max(1, len(task_stems | _content(other.task)))
            >= _NEAR_DUPLICATE
            for other in kept
        ):
            continue
        seen_quotes.add(normalized_quote)
        kept.append(Commitment(task=task, quote=sentence, day=spoken_day(sentence)))
        if len(kept) == MAX_COMMITMENTS:
            break
    return tuple(kept)


def _chunks(transcript: str) -> list[str]:
    if len(transcript) <= _CHUNK_CHARS:
        return [transcript]
    pieces: list[str] = []
    rest = transcript
    while rest and len(pieces) < _MAX_CHUNKS:
        if len(rest) <= _CHUNK_CHARS:
            pieces.append(rest)
            break
        cut = max(rest.rfind(". ", 0, _CHUNK_CHARS), rest.rfind("\n", 0, _CHUNK_CHARS))
        cut = cut + 1 if cut > _CHUNK_CHARS // 2 else _CHUNK_CHARS
        pieces.append(rest[:cut])
        rest = rest[cut:].lstrip()
    return pieces


async def extract_commitments(
    completer: _Completer, transcript: str,
) -> tuple[Commitment, ...] | None:
    """What the wearer said they will do, backed by the transcript's own words.

    None when the model never answered (a retry may work); an empty tuple when
    it answered and nothing the transcript backs up was left.
    """
    proposed: list[tuple[str, str]] = []
    answered = False
    for chunk in _chunks(transcript):
        raw = await completer.complete(system=COMMITMENT_SYSTEM, text=chunk, max_tokens=900)
        if raw is None or not raw.strip():
            continue
        answered = True
        if raw.strip().casefold().strip(".") != "none":
            proposed.extend(parse_blocks(raw))
    if not answered:
        return None
    found = keep_backed_commitments(proposed, transcript)
    log.info("commitments: %d proposed, %d kept", len(proposed), len(found))
    return found


__all__ = [
    "COMMITMENT_SYSTEM",
    "MAX_COMMITMENTS",
    "Commitment",
    "extract_commitments",
    "keep_backed_commitments",
    "parse_blocks",
    "spoken_day",
]
