"""Finished Brainstorms, kept so a restart does not erase the answer.

The snapshot that feeds the HUD panel lives in memory and dies with the
engine, so the synthesis a wearer walked away with was gone the next time
the Mac restarted — and the Recent list the design calls for had no
source at all.

Scope is deliberately narrow. This stores what the wearer can re-read:
the question, who sat, the turns, and the closing verdict. It is NOT a
transcript archive for the collaborators — replaying an old session into
a new prompt is a different feature with different privacy questions, and
the truncation policy it would need does not exist yet.

Content stays on this Mac. Nothing here is published to the glasses
beyond the rows the wearer asked to see, and acceptance receipts never
read it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_HISTORY_PATH = (
    Path.home() / "Library/Application Support/JOAT/brainstorms.json"
)
DEFAULT_EXPORT_DIR = Path.home() / "Documents/JOAT Brainstorms"
"""Where a finished session becomes a file the wearer can actually open.

Documents, not Application Support: the point of an export is that the
result LEAVES the glasses and is readable without JOAT running. A file
buried in a support directory is storage, not an export."""
KEEP = 20
"""How many sessions to retain. A glance-sized surface cannot show more,
and an unbounded file of wearer conversation is a liability, not a
feature."""


@dataclass(frozen=True)
class PastBrainstorm:
    """One finished session, as the wearer would recognise it."""

    topic: str
    speakers: tuple[str, ...]
    turns: tuple[tuple[str, str], ...]
    verdict: str
    at: float

    @property
    def title(self) -> str:
        """What the Recent list shows — the question, trimmed."""
        return (self.topic or "untitled").strip()[:44]

    @property
    def synthesis(self) -> str:
        """The JOAT turn if there is one, else the last thing said.

        The synthesis is what the wearer came back for; falling back to
        the final turn is better than an empty row when a session ended
        without one (cancelled, or too few collaborators).
        """
        for label, text in reversed(self.turns):
            if label.startswith("JOAT"):
                return text
        return self.turns[-1][1] if self.turns else ""

    def age(self, now: float | None = None) -> str:
        seconds = max(0.0, (time.time() if now is None else now) - self.at)
        if seconds < 90:
            return "just now"
        if seconds < 3600:
            return f"{int(seconds // 60)}m ago"
        if seconds < 86_400:
            return f"{int(seconds // 3600)}h ago"
        return f"{int(seconds // 86_400)}d ago"


class BrainstormHistory:
    """Append finished sessions; read the recent ones back."""

    def __init__(self, path: Path | None = None, keep: int = KEEP,
                 export_dir: Path | None = None) -> None:
        self._path = Path(path) if path is not None else DEFAULT_HISTORY_PATH
        self._keep = keep
        self._export_dir = (
            Path(export_dir) if export_dir is not None else DEFAULT_EXPORT_DIR
        )

    @property
    def export_dir(self) -> Path:
        return self._export_dir

    def record(self, topic: str, speakers: tuple[str, ...],
               turns: tuple[tuple[str, str], ...], verdict: str) -> None:
        """Keep a finished session. A session with no turns is not kept.

        Never raises: losing a history entry must not take down the
        session that just produced it, and the wearer already has the
        answer on the glasses.
        """
        if not turns:
            return
        entry = PastBrainstorm(
            topic=topic, speakers=tuple(speakers),
            turns=tuple(turns), verdict=verdict, at=time.time(),
        )
        try:
            kept = [*self._load(), entry][-self._keep:]
            self._write(kept)
        except Exception:
            log.warning("brainstorm history not written", exc_info=True)
        # The export is separate on purpose: a readable file is the thing
        # the wearer opens later, and losing one must not lose the other.
        try:
            written = self._export(entry)
            log.info("brainstorm exported: %s", written.name)
        except Exception:
            log.warning("brainstorm not exported", exc_info=True)

    def recent(self, limit: int = 5) -> list[PastBrainstorm]:
        """Newest first — the wearer is looking for what they just did."""
        try:
            return list(reversed(self._load()))[:limit]
        except Exception:
            log.warning("brainstorm history not readable", exc_info=True)
            return []

    def find(self, spoken: str) -> PastBrainstorm | None:
        """The session the wearer means, by however they say the question.

        Matched on the topic only. Searching the TURNS would mean scanning
        the collaborators' words for a phrase the wearer half-remembers,
        which reads far more of their conversation than answering the
        question requires.
        """
        said = " ".join(spoken.lower().split()).strip()
        if not said:
            return None
        history = self.recent(limit=self._keep)
        for past in history:                       # newest first
            topic = past.topic.lower()
            if topic == said or said in topic or topic in said:
                return past
        words = [w for w in said.split() if len(w) > 3]
        for past in history:
            topic = past.topic.lower()
            if words and all(word in topic for word in words):
                return past
        return None

    def rows(self, limit: int = 5, width: int = 46) -> list[str]:
        """Recent sessions as glance-sized rows for the HUD.

        The METADATA is budgeted first and the title takes what is left.
        Trimming the whole row instead cut the age off the end — and the
        age is what makes a list of past questions scannable at all.
        """
        rows: list[str] = []
        for past in self.recent(limit):
            meta = f" · {len(past.turns)} turns · {past.age()}"
            room = max(8, width - len(meta))
            title = past.title
            if len(title) > room:
                title = title[:room - 1].rstrip() + "…"
            rows.append(f"{title}{meta}")
        return rows

    def _export(self, entry: PastBrainstorm) -> Path:
        """Write one session as Markdown the wearer can open anywhere."""
        self._export_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d-%H%M", time.localtime(entry.at))
        slug = re.sub(r"[^a-z0-9]+", "-", entry.topic.lower()).strip("-")[:48]
        target = self._export_dir / f"{stamp}-{slug or 'brainstorm'}.md"
        lines = [
            f"# {entry.topic or 'Brainstorm'}",
            "",
            f"*{time.strftime('%d %b %Y, %H:%M', time.localtime(entry.at))} · "
            f"{', '.join(entry.speakers) or 'no seats'} · {entry.verdict}*",
            "",
        ]
        for label, text in entry.turns:
            lines += [f"## {label}", "", text.strip(), ""]
        target.write_text("\n".join(lines))
        return target

    # ── storage ──────────────────────────────────────────────────────────

    def _load(self) -> list[PastBrainstorm]:
        if not self._path.exists():
            return []
        raw = json.loads(self._path.read_text())
        if not isinstance(raw, list):
            return []
        out: list[PastBrainstorm] = []
        for record in raw:
            if not isinstance(record, dict):
                continue
            turns = record.get("turns")
            if not isinstance(turns, list):
                continue
            out.append(PastBrainstorm(
                topic=str(record.get("topic", "")),
                speakers=tuple(str(s) for s in record.get("speakers", [])),
                turns=tuple(
                    (str(t[0]), str(t[1])) for t in turns
                    if isinstance(t, (list, tuple)) and len(t) == 2
                ),
                verdict=str(record.get("verdict", "")),
                at=float(record.get("at", 0.0)),
            ))
        return out

    def _write(self, entries: list[PastBrainstorm]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            {
                "topic": e.topic, "speakers": list(e.speakers),
                "turns": [list(t) for t in e.turns],
                "verdict": e.verdict, "at": e.at,
            }
            for e in entries
        ]
        # Atomic: a half-written history is worse than a missing one, and
        # this is written at the end of a session the wearer just watched.
        handle, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(handle, "w") as out:
                json.dump(payload, out)
            os.replace(tmp, self._path)
        except BaseException:
            # The cleanup path must never mask the real error.
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


_READ_LAST = re.compile(
    r"^(?:read|show|what was)(?: me)?(?: the| my)? "
    r"(?:last|latest|previous) brainstorm$"
)
_RECALL = re.compile(
    r"^what did (?:we|you|they) (?:decide|conclude|say) about (?P<topic>.+)$"
)
_WHERE = re.compile(
    r"^(?:where|what)(?: are| is)?(?: my| the)? "
    r"(?:brainstorms?|brainstorm (?:exports?|files?|notes?))"
    r"(?: saved| stored| kept| go)?$"
)


def _recite(past: PastBrainstorm) -> str:
    """What the wearer came back for: the answer, then where it came from."""
    body = past.synthesis or "nothing was concluded"
    return f"“{past.title}”, {past.age()}:\n{body}"


class BrainstormHistoryCommands:
    """Answer 'where are my brainstorms' — the glasses are voice-first.

    An export nobody can find is not an export. The wearer asks on the
    glasses and gets the folder they will open on the Mac.
    """

    def __init__(self, history: BrainstormHistory) -> None:
        self._history = history

    async def handle_command(self, text: str) -> str | None:
        said = " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())
        if _READ_LAST.match(said):
            recent = self._history.recent(1)
            if not recent:
                return "No brainstorms yet."
            return self._recite(recent[0])
        recall = _RECALL.match(said)
        if recall is not None:
            past = self._history.find(recall.group("topic"))
            if past is None:
                return f"No brainstorm about {recall.group('topic')}."
            return self._recite(past)
        if not _WHERE.match(said):
            return None
        recent = self._history.recent(1)
        where = f"Saved in {self._history.export_dir.name}."
        if not recent:
            return f"No brainstorms yet. {where}"
        return f"{len(self._history.recent(99))} saved, newest “{recent[0].title}”. {where}"

    @staticmethod
    def _recite(past: PastBrainstorm) -> str:
        return _recite(past)
