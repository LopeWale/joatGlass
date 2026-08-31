"""Durable, loss-accounted local Voice Note records and raw-audio journal."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import cast

from .store import DurableStore


_ACTIVE_STATES = frozenset({"preparing", "recording", "paused", "finalizing"})
_FINAL_STATES = frozenset(
    {"saved", "saved_degraded", "interrupted", "discarded", "failed"}
)
_TRANSITIONS: dict[str, frozenset[str]] = {
    "preparing": frozenset({"recording", "failed", "discarded"}),
    "recording": frozenset(
        {"paused", "finalizing", "interrupted", "failed", "discarded"}
    ),
    "paused": frozenset(
        {"recording", "finalizing", "interrupted", "failed", "discarded"}
    ),
    "finalizing": frozenset({"saved", "saved_degraded", "interrupted", "failed"}),
}


class VoiceNoteError(RuntimeError):
    """Base class for durable Voice Note failures."""


class VoiceNoteConflictError(VoiceNoteError):
    """A replay or transition conflicts with already durable capture truth."""


class VoiceNoteNotFoundError(VoiceNoteError):
    """The requested capture does not exist."""


@dataclass(frozen=True, slots=True)
class VoiceNote:
    capture_id: str
    idempotency_key: str
    title: str | None
    source_kind: str
    state: str
    created_at_us: int
    updated_at_us: int
    started_at_us: int | None
    ended_at_us: int | None
    received_frames: int
    journaled_frames: int
    transcribed_segments: int
    gap_count: int
    raw_bytes: int
    raw_retained: bool
    completion_code: str | None


@dataclass(frozen=True, slots=True)
class VoiceNoteFrame:
    capture_id: str
    ordinal: int
    connection_generation: str
    seq: int
    source: str
    speaker: str | None
    received_at_us: int
    pcm_digest: bytes
    pcm: bytes


@dataclass(frozen=True, slots=True)
class VoiceNoteSegment:
    capture_id: str
    ordinal: int
    kind: str
    text: str
    first_frame_ordinal: int | None
    last_frame_ordinal: int | None
    created_at_us: int


@dataclass(frozen=True, slots=True)
class VoiceNoteInsights:
    """Derived distillation of one retained note — replaceable, never truth.

    Segments are capture-time evidence and freeze at completion; insights are
    a generated view over them, attributed to the engine that produced them so
    a wearer always knows whether a summary came from the local model or an
    explicitly requested cloud pass.
    """

    capture_id: str
    engine: str
    generated_at_us: int
    summary: str
    key_points: tuple[str, ...]
    todos: tuple[str, ...]


def _identifier(name: str, value: str, *, maximum: int = 256) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or "\x00" in value
    ):
        raise ValueError(f"{name} must be non-empty, unpadded, bounded text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8 text") from exc


def _timestamp(now_us: int) -> None:
    if isinstance(now_us, bool) or not isinstance(now_us, int) or now_us < 0:
        raise ValueError("timestamp must be a non-negative integer")


class VoiceNoteStore:
    """Persist capture truth before STT and retain explicit loss evidence."""

    _NOTE_COLUMNS = (
        "capture_id,idempotency_key,title,source_kind,state,created_at_us,"
        "updated_at_us,started_at_us,ended_at_us,received_frames,"
        "journaled_frames,transcribed_segments,gap_count,raw_bytes,"
        "raw_retained,completion_code"
    )

    def __init__(self, store: DurableStore) -> None:
        self._store = store

    def create(
        self,
        *,
        capture_id: str,
        idempotency_key: str,
        title: str,
        source_kind: str,
        now_us: int,
    ) -> VoiceNote:
        _identifier("capture_id", capture_id)
        _identifier("idempotency_key", idempotency_key)
        _identifier("title", title, maximum=200)
        _identifier("source_kind", source_kind, maximum=80)
        _timestamp(now_us)
        with self._store.transaction() as db:
            existing = db.execute(
                f"SELECT {self._NOTE_COLUMNS} FROM voice_notes "
                "WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                note = self._note(existing)
                if (
                    note.capture_id != capture_id
                    or note.title != title
                    or note.source_kind != source_kind
                ):
                    raise VoiceNoteConflictError(
                        "idempotency key already names a different Voice Note"
                    )
                return note
            try:
                db.execute(
                    "INSERT INTO voice_notes("
                    "capture_id,idempotency_key,title,source_kind,state,"
                    "created_at_us,updated_at_us) VALUES (?,?,?,?,?,?,?)",
                    (
                        capture_id,
                        idempotency_key,
                        title,
                        source_kind,
                        "preparing",
                        now_us,
                        now_us,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise VoiceNoteConflictError(
                    "Voice Note identity conflicts with durable state"
                ) from exc
            return self._get_with(db, capture_id)

    def get(self, capture_id: str) -> VoiceNote:
        _identifier("capture_id", capture_id)
        return self._get_with(self._store._db, capture_id)

    def active(self) -> tuple[VoiceNote, ...]:
        placeholders = ",".join("?" for _ in _ACTIVE_STATES)
        rows = self._store.execute(
            f"SELECT {self._NOTE_COLUMNS} FROM voice_notes "
            f"WHERE state IN ({placeholders}) ORDER BY created_at_us,capture_id",
            tuple(sorted(_ACTIVE_STATES)),
        ).fetchall()
        return tuple(self._note(row) for row in rows)

    def latest_retained(self) -> VoiceNote | None:
        """Newest non-discarded terminal note, including recoverable failures."""
        row = self._store.execute(
            f"SELECT {self._NOTE_COLUMNS} FROM voice_notes "
            "WHERE state IN ('saved','saved_degraded','interrupted','failed') "
            "AND title IS NOT NULL ORDER BY created_at_us DESC,capture_id DESC LIMIT 1"
        ).fetchone()
        return self._note(row) if row is not None else None

    def transition(
        self,
        capture_id: str,
        state: str,
        *,
        now_us: int,
        completion_code: str | None = None,
    ) -> VoiceNote:
        _identifier("capture_id", capture_id)
        _identifier("state", state, maximum=32)
        if completion_code is not None:
            _identifier("completion_code", completion_code, maximum=120)
        _timestamp(now_us)
        with self._store.transaction() as db:
            current = self._get_with(db, capture_id)
            if current.state == state:
                return current
            if state not in _TRANSITIONS.get(current.state, frozenset()):
                raise VoiceNoteConflictError(
                    f"Voice Note transition {current.state} -> {state} is not allowed"
                )
            if now_us < current.updated_at_us:
                raise VoiceNoteConflictError(
                    "transition timestamp precedes durable Voice Note state"
                )
            started_at = now_us if state == "recording" and current.started_at_us is None else current.started_at_us
            ended_at = now_us if state in _FINAL_STATES else None
            title = None if state == "discarded" else current.title
            db.execute(
                "UPDATE voice_notes SET state=?,title=?,updated_at_us=?,"
                "started_at_us=?,ended_at_us=?,completion_code=? WHERE capture_id=?",
                (
                    state,
                    title,
                    now_us,
                    started_at,
                    ended_at,
                    completion_code,
                    capture_id,
                ),
            )
            return self._get_with(db, capture_id)

    def append_frame(
        self,
        capture_id: str,
        *,
        connection_generation: str,
        seq: int,
        source: str,
        speaker: str | None,
        pcm: bytes,
        received_at_us: int,
    ) -> VoiceNoteFrame:
        _identifier("capture_id", capture_id)
        _identifier("connection_generation", connection_generation)
        _identifier("source", source, maximum=80)
        if speaker is not None:
            _identifier("speaker", speaker, maximum=120)
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise ValueError("seq must be a non-negative integer")
        if not isinstance(pcm, bytes) or not pcm:
            raise ValueError("pcm must be non-empty bytes")
        _timestamp(received_at_us)
        digest = hashlib.sha256(pcm).digest()
        with self._store.transaction() as db:
            current = self._get_with(db, capture_id)
            if current.state != "recording":
                raise VoiceNoteConflictError(
                    "audio can be journaled only while a Voice Note is recording"
                )
            existing = db.execute(
                "SELECT capture_id,ordinal,connection_generation,seq,source,speaker,"
                "received_at_us,pcm_digest,pcm FROM voice_note_frames "
                "WHERE capture_id=? AND connection_generation=? AND seq=?",
                (capture_id, connection_generation, seq),
            ).fetchone()
            if existing is not None:
                frame = self._frame(existing)
                if (
                    frame.pcm_digest != digest
                    or frame.source != source
                    or frame.speaker != speaker
                ):
                    raise VoiceNoteConflictError(
                        "wire frame replay contains different audio or metadata"
                    )
                return frame
            ordinal_row = db.execute(
                "SELECT COALESCE(MAX(ordinal),-1)+1 FROM voice_note_frames "
                "WHERE capture_id=?",
                (capture_id,),
            ).fetchone()
            assert ordinal_row is not None
            ordinal = cast(int, ordinal_row[0])
            db.execute(
                "INSERT INTO voice_note_frames("
                "capture_id,ordinal,connection_generation,seq,source,speaker,"
                "received_at_us,pcm_digest,pcm) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    capture_id,
                    ordinal,
                    connection_generation,
                    seq,
                    source,
                    speaker,
                    received_at_us,
                    digest,
                    pcm,
                ),
            )
            db.execute(
                "UPDATE voice_notes SET received_frames=received_frames+1,"
                "journaled_frames=journaled_frames+1,raw_bytes=raw_bytes+?,"
                "updated_at_us=MAX(updated_at_us,?) WHERE capture_id=?",
                (len(pcm), received_at_us, capture_id),
            )
            row = db.execute(
                "SELECT capture_id,ordinal,connection_generation,seq,source,speaker,"
                "received_at_us,pcm_digest,pcm FROM voice_note_frames "
                "WHERE capture_id=? AND ordinal=?",
                (capture_id, ordinal),
            ).fetchone()
            assert row is not None
            return self._frame(row)

    def append_gap(
        self,
        capture_id: str,
        *,
        code: str,
        detail: str,
        now_us: int,
    ) -> VoiceNoteSegment:
        _identifier("code", code, maximum=80)
        _identifier("detail", detail, maximum=300)
        return self._append_segment(
            capture_id,
            kind="gap",
            text=f"[{code}] {detail}",
            first_frame_ordinal=None,
            last_frame_ordinal=None,
            now_us=now_us,
        )

    def append_transcript(
        self,
        capture_id: str,
        *,
        text: str,
        first_frame_ordinal: int,
        last_frame_ordinal: int,
        now_us: int,
    ) -> VoiceNoteSegment:
        _identifier("transcript", text, maximum=20_000)
        if (
            isinstance(first_frame_ordinal, bool)
            or not isinstance(first_frame_ordinal, int)
            or first_frame_ordinal < 0
            or isinstance(last_frame_ordinal, bool)
            or not isinstance(last_frame_ordinal, int)
            or last_frame_ordinal < first_frame_ordinal
        ):
            raise ValueError("transcript frame range is invalid")
        return self._append_segment(
            capture_id,
            kind="transcript",
            text=text,
            first_frame_ordinal=first_frame_ordinal,
            last_frame_ordinal=last_frame_ordinal,
            now_us=now_us,
        )

    def append_system(
        self,
        capture_id: str,
        *,
        code: str,
        detail: str,
        now_us: int,
    ) -> VoiceNoteSegment:
        _identifier("code", code, maximum=80)
        _identifier("detail", detail, maximum=300)
        return self._append_segment(
            capture_id,
            kind="system",
            text=f"[{code}] {detail}",
            first_frame_ordinal=None,
            last_frame_ordinal=None,
            now_us=now_us,
        )

    def _append_segment(
        self,
        capture_id: str,
        *,
        kind: str,
        text: str,
        first_frame_ordinal: int | None,
        last_frame_ordinal: int | None,
        now_us: int,
    ) -> VoiceNoteSegment:
        _identifier("capture_id", capture_id)
        _timestamp(now_us)
        with self._store.transaction() as db:
            current = self._get_with(db, capture_id)
            if current.state not in {"recording", "paused", "finalizing"}:
                raise VoiceNoteConflictError(
                    "segments cannot be appended after Voice Note completion"
                )
            if kind == "transcript":
                assert first_frame_ordinal is not None
                assert last_frame_ordinal is not None
                expected = last_frame_ordinal - first_frame_ordinal + 1
                covered = db.execute(
                    "SELECT count(*) FROM voice_note_frames WHERE capture_id=? "
                    "AND ordinal BETWEEN ? AND ?",
                    (capture_id, first_frame_ordinal, last_frame_ordinal),
                ).fetchone()
                assert covered is not None
                if covered[0] != expected:
                    raise VoiceNoteConflictError(
                        "transcript range is not backed by journaled audio"
                    )
            ordinal_row = db.execute(
                "SELECT COALESCE(MAX(ordinal),-1)+1 FROM voice_note_segments "
                "WHERE capture_id=?",
                (capture_id,),
            ).fetchone()
            assert ordinal_row is not None
            ordinal = cast(int, ordinal_row[0])
            db.execute(
                "INSERT INTO voice_note_segments("
                "capture_id,ordinal,kind,text,first_frame_ordinal,"
                "last_frame_ordinal,created_at_us) VALUES (?,?,?,?,?,?,?)",
                (
                    capture_id,
                    ordinal,
                    kind,
                    text,
                    first_frame_ordinal,
                    last_frame_ordinal,
                    now_us,
                ),
            )
            if kind == "transcript":
                db.execute(
                    "UPDATE voice_notes SET transcribed_segments="
                    "transcribed_segments+1,updated_at_us=MAX(updated_at_us,?) "
                    "WHERE capture_id=?",
                    (now_us, capture_id),
                )
            elif kind == "gap":
                db.execute(
                    "UPDATE voice_notes SET gap_count=gap_count+1,"
                    "updated_at_us=MAX(updated_at_us,?) WHERE capture_id=?",
                    (now_us, capture_id),
                )
            return VoiceNoteSegment(
                capture_id=capture_id,
                ordinal=ordinal,
                kind=kind,
                text=text,
                first_frame_ordinal=first_frame_ordinal,
                last_frame_ordinal=last_frame_ordinal,
                created_at_us=now_us,
            )

    def finalize(
        self,
        capture_id: str,
        *,
        complete: bool,
        completion_code: str,
        now_us: int,
    ) -> VoiceNote:
        _identifier("completion_code", completion_code, maximum=120)
        _timestamp(now_us)
        with self._store.transaction() as db:
            current = self._get_with(db, capture_id)
            if current.state != "finalizing":
                raise VoiceNoteConflictError(
                    "Voice Note must be finalizing before it can be saved"
                )
            if now_us < current.updated_at_us:
                raise VoiceNoteConflictError(
                    "finalization timestamp precedes durable Voice Note state"
                )
            covered_row = db.execute(
                "SELECT count(DISTINCT frame.ordinal) "
                "FROM voice_note_frames AS frame "
                "JOIN voice_note_segments AS segment "
                "ON segment.capture_id=frame.capture_id "
                "AND segment.kind='transcript' "
                "AND frame.ordinal BETWEEN segment.first_frame_ordinal "
                "AND segment.last_frame_ordinal "
                "WHERE frame.capture_id=?",
                (capture_id,),
            ).fetchone()
            assert covered_row is not None
            transcribed_frames = cast(int, covered_row[0])
            proven_complete = (
                complete
                and current.received_frames == current.journaled_frames
                and current.gap_count == 0
                and (
                    current.received_frames == 0
                    or transcribed_frames == current.journaled_frames
                )
            )
            state = "saved" if proven_complete else "saved_degraded"
            if proven_complete:
                db.execute(
                    "DELETE FROM voice_note_frames WHERE capture_id=?",
                    (capture_id,),
                )
            db.execute(
                "UPDATE voice_notes SET state=?,updated_at_us=?,ended_at_us=?,"
                "completion_code=?,raw_bytes=?,raw_retained=? WHERE capture_id=?",
                (
                    state,
                    now_us,
                    now_us,
                    completion_code,
                    0 if proven_complete else current.raw_bytes,
                    0 if proven_complete else 1,
                    capture_id,
                ),
            )
            return self._get_with(db, capture_id)

    def discard(self, capture_id: str, *, now_us: int) -> VoiceNote:
        _timestamp(now_us)
        with self._store.transaction() as db:
            current = self._get_with(db, capture_id)
            if current.state not in {"preparing", "recording", "paused"}:
                raise VoiceNoteConflictError(
                    "only an unfinished Voice Note can be discarded"
                )
            if now_us < current.updated_at_us:
                raise VoiceNoteConflictError(
                    "discard timestamp precedes durable Voice Note state"
                )
            db.execute("DELETE FROM voice_note_frames WHERE capture_id=?", (capture_id,))
            db.execute("DELETE FROM voice_note_segments WHERE capture_id=?", (capture_id,))
            db.execute(
                "UPDATE voice_notes SET title=NULL,state='discarded',updated_at_us=?,"
                "ended_at_us=?,received_frames=0,journaled_frames=0,"
                "transcribed_segments=0,gap_count=0,raw_bytes=0,raw_retained=0,"
                "completion_code='wearer_discarded_content_purged' WHERE capture_id=?",
                (now_us, now_us, capture_id),
            )
            return self._get_with(db, capture_id)

    def recover_after_restart(self, *, now_us: int) -> tuple[str, ...]:
        _timestamp(now_us)
        with self._store.transaction() as db:
            rows = db.execute(
                "SELECT capture_id FROM voice_notes WHERE state IN "
                "('preparing','recording','paused','finalizing') "
                "ORDER BY created_at_us,capture_id"
            ).fetchall()
            capture_ids = tuple(cast(str, row[0]) for row in rows)
            if capture_ids:
                db.execute(
                    "UPDATE voice_notes SET state='interrupted',"
                    "updated_at_us=MAX(updated_at_us,?),"
                    "ended_at_us=MAX(updated_at_us,?),"
                    "completion_code='process_restart_raw_retained' "
                    "WHERE state IN ('preparing','recording','paused','finalizing')",
                    (now_us, now_us),
                )
            return capture_ids

    def frame_count(self, capture_id: str) -> int:
        _identifier("capture_id", capture_id)
        row = self._store.execute(
            "SELECT count(*) FROM voice_note_frames WHERE capture_id=?",
            (capture_id,),
        ).fetchone()
        assert row is not None
        return cast(int, row[0])

    def frames(self, capture_id: str) -> tuple[VoiceNoteFrame, ...]:
        """Return retained raw frames in durable capture order."""
        _identifier("capture_id", capture_id)
        rows = self._store.execute(
            "SELECT capture_id,ordinal,connection_generation,seq,source,speaker,"
            "received_at_us,pcm_digest,pcm FROM voice_note_frames "
            "WHERE capture_id=? ORDER BY ordinal",
            (capture_id,),
        ).fetchall()
        return tuple(self._frame(row) for row in rows)

    def segments(self, capture_id: str) -> tuple[VoiceNoteSegment, ...]:
        _identifier("capture_id", capture_id)
        rows = self._store.execute(
            "SELECT capture_id,ordinal,kind,text,first_frame_ordinal,"
            "last_frame_ordinal,created_at_us FROM voice_note_segments "
            "WHERE capture_id=? ORDER BY ordinal",
            (capture_id,),
        ).fetchall()
        return tuple(self._segment(row) for row in rows)

    _RETAINED_STATES = ("saved", "saved_degraded", "interrupted")

    def recent_retained(self, limit: int = 5) -> tuple[VoiceNote, ...]:
        """Newest titled, non-discarded terminal notes for the HUD list."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        placeholders = ",".join("?" for _ in self._RETAINED_STATES)
        rows = self._store.execute(
            f"SELECT {self._NOTE_COLUMNS} FROM voice_notes "
            f"WHERE state IN ({placeholders}) AND title IS NOT NULL "
            "ORDER BY updated_at_us DESC,capture_id DESC LIMIT ?",
            (*self._RETAINED_STATES, limit),
        ).fetchall()
        return tuple(self._note(row) for row in rows)

    def replace_insights(
        self,
        capture_id: str,
        *,
        engine: str,
        summary: str,
        key_points: tuple[str, ...],
        todos: tuple[str, ...],
        now_us: int,
    ) -> VoiceNoteInsights:
        """Atomically replace the derived distillation of one retained note.

        Insights never claim capture truth, so unlike segments they may be
        regenerated after completion — but only for a retained note that has
        at least one transcript section to distill from.
        """
        _identifier("capture_id", capture_id)
        _identifier("engine", engine, maximum=64)
        _timestamp(now_us)
        cleaned_summary = " ".join(summary.split())
        if not cleaned_summary or len(cleaned_summary) > 4_000:
            raise ValueError("summary must be 1 to 4000 visible characters")
        cleaned_points = self._clean_insight_lines("key_points", key_points)
        cleaned_todos = self._clean_insight_lines("todos", todos)
        with self._store.transaction() as db:
            current = self._get_with(db, capture_id)
            if current.state not in self._RETAINED_STATES:
                raise VoiceNoteConflictError(
                    "insights require a retained Voice Note"
                )
            if current.transcribed_segments < 1:
                raise VoiceNoteConflictError(
                    "insights require at least one transcript section"
                )
            db.execute(
                "DELETE FROM voice_note_insights WHERE capture_id=?",
                (capture_id,),
            )
            rows = [
                (capture_id, "summary", 0, cleaned_summary, engine, now_us),
                *(
                    (capture_id, "key_point", ordinal, text, engine, now_us)
                    for ordinal, text in enumerate(cleaned_points)
                ),
                *(
                    (capture_id, "todo", ordinal, text, engine, now_us)
                    for ordinal, text in enumerate(cleaned_todos)
                ),
            ]
            db.executemany(
                "INSERT INTO voice_note_insights("
                "capture_id,kind,ordinal,text,engine,generated_at_us) "
                "VALUES (?,?,?,?,?,?)",
                rows,
            )
            db.execute(
                "UPDATE voice_notes SET updated_at_us=MAX(updated_at_us,?) "
                "WHERE capture_id=?",
                (now_us, capture_id),
            )
        return VoiceNoteInsights(
            capture_id=capture_id,
            engine=engine,
            generated_at_us=now_us,
            summary=cleaned_summary,
            key_points=cleaned_points,
            todos=cleaned_todos,
        )

    def insights(self, capture_id: str) -> VoiceNoteInsights | None:
        _identifier("capture_id", capture_id)
        # Existence check keeps an unknown id an error rather than a None.
        self._get_with(self._store._db, capture_id)
        rows = self._store.execute(
            "SELECT kind,ordinal,text,engine,generated_at_us "
            "FROM voice_note_insights WHERE capture_id=? ORDER BY kind,ordinal",
            (capture_id,),
        ).fetchall()
        if not rows:
            return None
        summary = ""
        engine = ""
        generated_at_us = 0
        key_points: list[str] = []
        todos: list[str] = []
        for row in rows:
            kind = cast(str, row[0])
            text = cast(str, row[2])
            engine = cast(str, row[3])
            generated_at_us = cast(int, row[4])
            if kind == "summary":
                summary = text
            elif kind == "key_point":
                key_points.append(text)
            else:
                todos.append(text)
        return VoiceNoteInsights(
            capture_id=capture_id,
            engine=engine,
            generated_at_us=generated_at_us,
            summary=summary,
            key_points=tuple(key_points),
            todos=tuple(todos),
        )

    @staticmethod
    def _clean_insight_lines(name: str, lines: tuple[str, ...]) -> tuple[str, ...]:
        cleaned: list[str] = []
        for line in lines:
            text = " ".join(line.split())
            if not text:
                continue
            if len(text) > 500:
                raise ValueError(f"each of {name} must be at most 500 characters")
            cleaned.append(text)
        if len(cleaned) > 12:
            raise ValueError(f"{name} must contain at most 12 entries")
        return tuple(cleaned)

    @classmethod
    def _get_with(cls, db: sqlite3.Connection, capture_id: str) -> VoiceNote:
        row = db.execute(
            f"SELECT {cls._NOTE_COLUMNS} FROM voice_notes WHERE capture_id=?",
            (capture_id,),
        ).fetchone()
        if row is None:
            raise VoiceNoteNotFoundError("unknown Voice Note")
        return cls._note(row)

    @staticmethod
    def _note(row: tuple[object, ...] | sqlite3.Row) -> VoiceNote:
        return VoiceNote(
            capture_id=cast(str, row[0]),
            idempotency_key=cast(str, row[1]),
            title=cast(str, row[2]) if row[2] is not None else None,
            source_kind=cast(str, row[3]),
            state=cast(str, row[4]),
            created_at_us=cast(int, row[5]),
            updated_at_us=cast(int, row[6]),
            started_at_us=cast(int, row[7]) if row[7] is not None else None,
            ended_at_us=cast(int, row[8]) if row[8] is not None else None,
            received_frames=cast(int, row[9]),
            journaled_frames=cast(int, row[10]),
            transcribed_segments=cast(int, row[11]),
            gap_count=cast(int, row[12]),
            raw_bytes=cast(int, row[13]),
            raw_retained=bool(row[14]),
            completion_code=cast(str, row[15]) if row[15] is not None else None,
        )

    @staticmethod
    def _frame(row: tuple[object, ...] | sqlite3.Row) -> VoiceNoteFrame:
        return VoiceNoteFrame(
            capture_id=cast(str, row[0]),
            ordinal=cast(int, row[1]),
            connection_generation=cast(str, row[2]),
            seq=cast(int, row[3]),
            source=cast(str, row[4]),
            speaker=cast(str, row[5]) if row[5] is not None else None,
            received_at_us=cast(int, row[6]),
            pcm_digest=cast(bytes, row[7]),
            pcm=cast(bytes, row[8]),
        )

    @staticmethod
    def _segment(row: tuple[object, ...] | sqlite3.Row) -> VoiceNoteSegment:
        return VoiceNoteSegment(
            capture_id=cast(str, row[0]),
            ordinal=cast(int, row[1]),
            kind=cast(str, row[2]),
            text=cast(str, row[3]),
            first_frame_ordinal=(cast(int, row[4]) if row[4] is not None else None),
            last_frame_ordinal=(cast(int, row[5]) if row[5] is not None else None),
            created_at_us=cast(int, row[6]),
        )


__all__ = [
    "VoiceNote",
    "VoiceNoteConflictError",
    "VoiceNoteError",
    "VoiceNoteFrame",
    "VoiceNoteInsights",
    "VoiceNoteNotFoundError",
    "VoiceNoteSegment",
    "VoiceNoteStore",
]
