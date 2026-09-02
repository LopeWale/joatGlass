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
import secrets
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


def _status_from_verdict(verdict: str) -> str:
    """The machine-readable outcome, for records written before it existed.

    ``verdict`` is prose aimed at the wearer ("done — converged",
    "cancelled", "blocked · collaborator disconnected"), so migrating an
    old row means reading it. Anything not recognisably an ending counts
    as settled: a session in the history file is by definition finished.
    """
    lowered = verdict.strip().lower()
    if lowered.startswith("cancelled"):
        return "cancelled"
    return "settled"


@dataclass(frozen=True)
class PastBrainstorm:
    """One session, as the durable record docs/SESSION_MODEL.md:30-35 asks for.

    That list is: an opaque local session ID, title and creation/update
    time, root and parent branch IDs, the ordered turns, verification state
    and latest synthesis, and an ``active``/``settled``/``cancelled``
    status. None of the identity fields existed, which is why nothing could
    SELECT a past session — there was no id to select. A list of rows the
    wearer cannot open is not a list, and the recent-session picker the
    spec calls for at :47 had no record to build on.
    """

    topic: str
    speakers: tuple[str, ...]
    turns: tuple[tuple[str, str], ...]
    verdict: str
    created_at: float
    #: Opaque and local. Never derived from wearer content, so it can be
    #: spoken, logged, or put on the wire without carrying the question.
    session_id: str = ""
    updated_at: float = 0.0
    #: Lineage. ``root_id`` is the first session of a branch family and
    #: equals ``session_id`` for an unbranched one; ``parent_id`` is the
    #: session this one forked from. SESSION_MODEL.md:51 specifies
    #: "branch this"; these are the fields it will need.
    root_id: str = ""
    parent_id: str = ""
    #: SESSION_MODEL.md:35. ``verdict`` is prose for the wearer; this is
    #: the machine-readable outcome the picker filters and labels on.
    status: str = "settled"
    #: Whether the verifier passed every claim it checked. ``None`` means
    #: nothing needed checking — which is most thinking sessions, and is
    #: NOT the same as "failed".
    verified: bool | None = None

    @property
    def at(self) -> float:
        """When this session last changed. Retained under the old name so
        every existing caller (age, rows, export) keeps working."""
        return self.updated_at or self.created_at

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

    def record(
        self,
        topic: str,
        speakers: tuple[str, ...],
        turns: tuple[tuple[str, str], ...],
        verdict: str,
        *,
        session_id: str | None = None,
        parent_id: str = "",
        root_id: str = "",
        verified: bool | None = None,
    ) -> None:
        """Keep a finished session. A session with no turns is not kept.

        Never raises: losing a history entry must not take down the
        session that just produced it, and the wearer already has the
        answer on the glasses.
        """
        if not turns:
            return
        now = time.time()
        # Opaque: 16 hex characters of os.urandom, never derived from the
        # topic. An id that encodes wearer content is not an id, it is the
        # content in another shape — and this one is spoken back, logged,
        # and eventually put on the wire to the glasses.
        identifier = session_id or secrets.token_hex(8)
        entry = PastBrainstorm(
            topic=topic, speakers=tuple(speakers),
            turns=tuple(turns), verdict=verdict,
            created_at=now, updated_at=now,
            session_id=identifier,
            # An unbranched session is its own root, so lineage is always
            # answerable without a special case for "no parent".
            root_id=root_id or identifier,
            parent_id=parent_id,
            status=_status_from_verdict(verdict),
            verified=verified,
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

    def begin(
        self,
        topic: str,
        speakers: tuple[str, ...],
        *,
        session_id: str,
        parent_id: str = "",
        root_id: str = "",
    ) -> None:
        """Open the durable record the moment the session starts.

        docs/SESSION_MODEL.md:35 lists an ``active`` status, and no record
        was ever written with it — the only writer ran at close, so a
        session that crashed or a Mac that lost power left no trace that a
        conversation had ever been happening. Identity is durable from
        birth; ``finish`` updates this row rather than appending a second.
        Never raises.
        """
        if not session_id:
            return
        now = time.time()
        entry = PastBrainstorm(
            topic=topic, speakers=tuple(speakers), turns=(),
            verdict="", created_at=now, updated_at=now,
            session_id=session_id,
            root_id=root_id or session_id, parent_id=parent_id,
            status="active",
        )
        try:
            kept = [p for p in self._load() if p.session_id != session_id]
            self._write([*kept, entry][-self._keep:])
        except Exception:
            log.warning("brainstorm begin not written", exc_info=True)

    def finish(
        self,
        session_id: str,
        turns: tuple[tuple[str, str], ...],
        verdict: str,
        *,
        verified: bool | None = None,
    ) -> None:
        """Settle the active record ``begin`` opened. Never raises.

        Falls back to appending a fresh row when the begin was never
        written (a full disk at open, an old caller), so the answer the
        wearer walked away with is kept either way. A session with no
        turns is DELETED rather than settled: an empty record cannot be
        reopened into anything.
        """
        try:
            kept = self._load()
        except Exception:
            log.warning("brainstorm history not readable at finish", exc_info=True)
            return
        existing = next(
            (p for p in kept if session_id and p.session_id == session_id), None
        )
        if not turns:
            if existing is not None:
                try:
                    self._write([p for p in kept if p.session_id != session_id])
                except Exception:
                    log.warning("empty brainstorm not removed", exc_info=True)
            return
        if existing is None:
            self.record(
                "untitled", (), turns, verdict,
                session_id=session_id, verified=verified,
            )
            return
        entry = PastBrainstorm(
            topic=existing.topic, speakers=existing.speakers,
            turns=tuple(turns), verdict=verdict,
            created_at=existing.created_at, updated_at=time.time(),
            session_id=existing.session_id,
            root_id=existing.root_id, parent_id=existing.parent_id,
            status=_status_from_verdict(verdict), verified=verified,
        )
        try:
            self._write([entry if p.session_id == session_id else p for p in kept])
        except Exception:
            log.warning("brainstorm finish not written", exc_info=True)
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

    def sessions(self, limit: int = KEEP) -> list[PastBrainstorm]:
        """Newest first, for the recent-session picker.

        `recent()` already returns these rows; this name exists because the
        picker asks a different question — "what can I OPEN?" — and the
        answer is only useful now that every row carries an id. (Not named
        `list`: inside this class that shadows the builtin.)
        """
        return self.recent(limit)

    def load(self, session_id: str) -> PastBrainstorm | None:
        """One session by its opaque id — the operation the HUD needs.

        Without this, twenty stored sessions with full turns were reachable
        only as four lines of dead text on the glasses: the wearer could
        SEE that past sessions existed and could not open one. The picker,
        `continue <title>` and `branch this` (SESSION_MODEL.md:49-51) all
        land here.
        """
        if not session_id:
            return None
        for past in self.recent(limit=self._keep):
            if past.session_id == session_id:
                return past
        return None

    def find(self, spoken: str) -> PastBrainstorm | None:
        """The single best match — the newest. Use ``find_all`` where the
        spec requires ASKING on ambiguity rather than silently taking the
        newest (docs/SESSION_MODEL.md:49)."""
        matches = self.find_all(spoken)
        return matches[0] if matches else None

    def find_all(self, spoken: str) -> list[PastBrainstorm]:
        """Every session the spoken title could mean, newest first.

        Matched on the topic only. Searching the TURNS would mean scanning
        the collaborators' words for a phrase the wearer half-remembers,
        which reads far more of their conversation than answering the
        question requires.
        """
        said = " ".join(spoken.lower().split()).strip()
        if not said:
            return []
        history = self.recent(limit=self._keep)
        matches: list[PastBrainstorm] = []
        for past in history:                       # newest first
            topic = past.topic.lower()
            if topic == said or said in topic or topic in said:
                matches.append(past)
        words = [w for w in said.split() if len(w) > 3]
        for past in history:
            if past in matches:
                continue
            topic = past.topic.lower()
            if words and all(word in topic for word in words):
                matches.append(past)
        return matches

    def rows(self, limit: int = 5, width: int = 46) -> list[str]:
        """Recent sessions as glance-sized rows for the HUD.

        The METADATA is budgeted first and the title takes what is left.
        Trimming the whole row instead cut the age off the end — and the
        age is what makes a list of past questions scannable at all.
        """
        rows: list[str] = []
        for past in self.recent(limit):
            count = len(past.turns)
            meta = f" · {count} turn{'' if count == 1 else 's'} · {past.age()}"
            room = max(8, width - len(meta))
            title = past.title
            if len(title) > room:
                title = title[:room - 1].rstrip() + "…"
            rows.append(f"{title}{meta}")
        return rows

    def _export(self, entry: PastBrainstorm) -> Path:
        """Write one session as Markdown the wearer can open anywhere.

        Readable BY THE WEARER, not by the machine. The export is the point
        — a session they can open later — but it is also a second complete
        verbatim copy of the conversation, and it landed at the umask
        default (0644) while the private store it duplicates is 0600.
        An audit's counter-pass caught the asymmetry: SESSION_MODEL.md:37
        calls that store private, and a world-readable twin of it in
        ~/Documents makes the word do no work. Same mode for both copies;
        the directory too, since its NAMES are the session topics.
        """
        self._export_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            self._export_dir.chmod(0o700)
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
        with contextlib.suppress(OSError):
            target.chmod(0o600)
        return target

    # ── storage ──────────────────────────────────────────────────────────

    def _load(self) -> list[PastBrainstorm]:
        if not self._path.exists():
            return []
        raw = json.loads(self._path.read_text())
        if not isinstance(raw, list):
            return []
        out: list[PastBrainstorm] = []
        for index, record in enumerate(raw):
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
                created_at=float(record.get("createdAt") or record.get("at") or 0.0),
                updated_at=float(record.get("at") or 0.0),
                # Sessions written before the durable record existed have no
                # id. Deriving one from a hash would leak the topic into the
                # identifier, so a stable synthetic id is minted from the
                # record's POSITION and timestamp instead, and persisted on
                # the next write. An old row stays selectable either way.
                session_id=str(record.get("id") or f"legacy-{index:02d}"),
                root_id=str(record.get("rootId") or record.get("id") or f"legacy-{index:02d}"),
                parent_id=str(record.get("parentId", "")),
                status=str(record.get("status") or _status_from_verdict(
                    str(record.get("verdict", "")))),
                verified=record.get("verified") if isinstance(
                    record.get("verified"), bool) else None,
            ))
        return out

    def _write(self, entries: list[PastBrainstorm]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            {
                "id": e.session_id,
                "rootId": e.root_id or e.session_id,
                "parentId": e.parent_id,
                "topic": e.topic, "speakers": list(e.speakers),
                "turns": [list(t) for t in e.turns],
                "verdict": e.verdict,
                "status": e.status,
                "verified": e.verified,
                "createdAt": e.created_at,
                "at": e.at,
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
