"""Durable local state for wearer-owned Brainstorm conversations.

Brainstorm content stays on the selected JOAT Mac.  Simulator and physical-G2
acceptance receipts consume only bounded metadata and frame digests; they never
read this store.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Literal


BrainstormStatus = Literal["active", "settled", "cancelled"]
BrainstormSpeaker = Literal["YOU", "CODEX", "CLAUDE", "VERIFY", "JOAT"]
VerificationState = Literal["pending", "verified", "uncertain", "repaired"]


@dataclass(frozen=True, slots=True)
class BrainstormTurn:
    speaker: BrainstormSpeaker
    text: str
    verification: VerificationState = "pending"


@dataclass(frozen=True, slots=True)
class BrainstormRecord:
    session_id: str
    title: str
    branch_parent_id: str | None
    branch_root_id: str
    turns: tuple[BrainstormTurn, ...] = field(default_factory=tuple)
    verification_state: VerificationState = "pending"
    synthesis: str = ""
    status: BrainstormStatus = "active"
    created_at_us: int = 0
    updated_at_us: int = 0


class BrainstormStore:
    """Small single-process JSON store with private, atomic replacement."""

    _SCHEMA_VERSION = 1

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def create(
        self,
        topic: str,
        *,
        parent_session_id: str | None = None,
    ) -> BrainstormRecord:
        title = " ".join(topic.split()).strip()
        if not title:
            raise ValueError("Brainstorm topic must not be empty")
        title = title[:80]
        now = time.time_ns() // 1_000
        session_id = f"brainstorm-{uuid.uuid4().hex}"
        root_id = session_id
        if parent_session_id is not None:
            parent = self.get(parent_session_id)
            if parent is None:
                raise KeyError(parent_session_id)
            root_id = parent.branch_root_id
        record = BrainstormRecord(
            session_id=session_id,
            title=title,
            branch_parent_id=parent_session_id,
            branch_root_id=root_id,
            created_at_us=now,
            updated_at_us=now,
        )
        records = self._load()
        records[session_id] = record
        self._save(records)
        return record

    def get(self, session_id: str) -> BrainstormRecord | None:
        return self._load().get(session_id)

    def recent(self, *, limit: int = 6) -> list[BrainstormRecord]:
        if limit < 1:
            return []
        return sorted(
            self._load().values(),
            key=lambda record: (record.updated_at_us, record.created_at_us, record.session_id),
            reverse=True,
        )[:limit]

    def append_turn(
        self,
        session_id: str,
        speaker: BrainstormSpeaker,
        text: str,
        *,
        verification: VerificationState = "pending",
    ) -> BrainstormRecord:
        body = " ".join(text.split()).strip()
        if not body:
            raise ValueError("Brainstorm turn must not be empty")
        records = self._load()
        current = self._require(records, session_id)
        updated = replace(
            current,
            turns=current.turns + (BrainstormTurn(speaker, body, verification),),
            status="active",
            updated_at_us=self._now_after(current.updated_at_us),
        )
        records[session_id] = updated
        self._save(records)
        return updated

    def settle(
        self,
        session_id: str,
        *,
        verification_state: VerificationState,
        synthesis: str,
    ) -> BrainstormRecord:
        records = self._load()
        current = self._require(records, session_id)
        updated = replace(
            current,
            verification_state=verification_state,
            synthesis=" ".join(synthesis.split()).strip(),
            status="settled",
            updated_at_us=self._now_after(current.updated_at_us),
        )
        records[session_id] = updated
        self._save(records)
        return updated

    def cancel(self, session_id: str) -> BrainstormRecord:
        records = self._load()
        current = self._require(records, session_id)
        updated = replace(
            current,
            status="cancelled",
            updated_at_us=self._now_after(current.updated_at_us),
        )
        records[session_id] = updated
        self._save(records)
        return updated

    def branch(self, session_id: str, title: str) -> BrainstormRecord:
        parent = self.get(session_id)
        if parent is None:
            raise KeyError(session_id)
        branch = self.create(title, parent_session_id=session_id)
        records = self._load()
        copied = replace(
            branch,
            turns=parent.turns,
            verification_state=parent.verification_state,
            synthesis=parent.synthesis,
        )
        records[branch.session_id] = copied
        self._save(records)
        return copied

    @staticmethod
    def _require(
        records: dict[str, BrainstormRecord],
        session_id: str,
    ) -> BrainstormRecord:
        try:
            return records[session_id]
        except KeyError:
            raise KeyError(session_id) from None

    @staticmethod
    def _now_after(previous: int) -> int:
        return max(time.time_ns() // 1_000, previous + 1)

    def _load(self) -> dict[str, BrainstormRecord]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if raw.get("schemaVersion") != self._SCHEMA_VERSION:
            raise ValueError("unsupported Brainstorm store schema")
        records: dict[str, BrainstormRecord] = {}
        for item in raw.get("sessions", []):
            turns = tuple(BrainstormTurn(**turn) for turn in item.get("turns", []))
            records[item["session_id"]] = BrainstormRecord(
                **{key: value for key, value in item.items() if key != "turns"},
                turns=turns,
            )
        return records

    def _save(self, records: dict[str, BrainstormRecord]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._path.parent, 0o700)
        payload = {
            "schemaVersion": self._SCHEMA_VERSION,
            "sessions": [asdict(record) for record in records.values()],
        }
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            dir=self._path.parent,
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
            os.chmod(self._path, 0o600)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


__all__ = [
    "BrainstormRecord",
    "BrainstormSpeaker",
    "BrainstormStatus",
    "BrainstormStore",
    "BrainstormTurn",
    "VerificationState",
]
