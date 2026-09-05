"""Resolve phone numbers and emails to contact names.

"+14052093077 · Messages" tells the wearer nothing at a glance; "Dele ·
Messages" tells them everything. Messages' own store only carries the raw
handle, so the name has to come from Contacts.

Read-only and immutable, like every other store this project touches. The
whole table is small (hundreds of rows), so it is loaded once and cached —
a per-notification query would be wasteful and would hold the file open.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import sqlite3

log = logging.getLogger(__name__)

_SOURCES = (
    "~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb",
    "~/Library/Application Support/AddressBook/AddressBook-v22.abcddb",
)


def normalize_handle(handle: str) -> str:
    """Comparable form of a phone number; emails pass through lowercased.

    Numbers appear in Contacts as "(405) 209-3077" and in Messages as
    "+14052093077", so both collapse to digits. The last 10 are the
    comparable part — country codes are present in one source and absent
    from the other.
    """
    if "@" in handle:
        return handle.strip().lower()
    digits = re.sub(r"\D", "", handle)
    return digits[-10:] if len(digits) >= 10 else digits


def pretty_number(handle: str) -> str:
    """Fallback display when no contact matches: +1 (405) 209-3077."""
    digits = re.sub(r"\D", "", handle)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return handle


class ContactResolver:
    """Normalized handle -> (display name, original handle), cached per process."""

    def __init__(self) -> None:
        self._map: dict[str, tuple[str, str]] | None = None

    def _load(self) -> dict[str, tuple[str, str]]:
        out: dict[str, tuple[str, str]] = {}
        for pattern in _SOURCES:
            for path in glob.glob(os.path.expanduser(pattern)):
                try:
                    conn = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
                except sqlite3.Error:
                    continue
                with conn:
                    for table, column in (("ZABCDPHONENUMBER", "ZFULLNUMBER"),
                                          ("ZABCDEMAILADDRESS", "ZADDRESS")):
                        try:
                            rows = conn.execute(f"""
                                SELECT p.{column},
                                       COALESCE(r.ZFIRSTNAME, ''),
                                       COALESCE(r.ZLASTNAME, ''),
                                       COALESCE(r.ZORGANIZATION, '')
                                FROM {table} p
                                JOIN ZABCDRECORD r ON p.ZOWNER = r.Z_PK
                            """).fetchall()
                        except sqlite3.Error:
                            continue  # schema drift: skip this table, keep the rest
                        for value, first, last, org in rows:
                            if not value:
                                continue
                            name = " ".join(p for p in (first, last) if p) or org
                            if not name:
                                continue
                            key = normalize_handle(str(value))
                            if key:
                                out.setdefault(key, (name, str(value)))
        log.info("contacts loaded: %d handles", len(out))
        return out

    def _loaded(self) -> dict[str, tuple[str, str]]:
        if self._map is None:
            try:
                self._map = self._load()
            except Exception:
                log.warning("contacts unavailable — showing raw handles")
                self._map = {}
        return self._map

    def name_for(self, handle: str) -> str:
        """A person's name when known, otherwise a readable number."""
        if not handle:
            return ""
        resolved = self._loaded().get(normalize_handle(handle))
        return resolved[0] if resolved is not None else pretty_number(handle)

    def search(self, query: str) -> list[tuple[str, str]]:
        """Mac-local candidates for a spoken recipient; never guess one.

        An exact full-name match wins. Otherwise every query word must prefix
        one word in the contact's display name. Multiple matches are returned
        to the caller so consequential send code can fail closed.
        """
        words = tuple(part for part in query.casefold().split() if part)
        if not words:
            return []
        candidates = sorted(
            set(self._loaded().values()),
            key=lambda item: (item[0].casefold(), normalize_handle(item[1])),
        )
        exact = [item for item in candidates if item[0].casefold() == " ".join(words)]
        if exact:
            return exact
        return [
            item for item in candidates
            if all(
                any(name_word.startswith(query_word)
                    for name_word in item[0].casefold().split())
                for query_word in words
            )
        ]


RELATIONSHIPS_PATH = os.path.expanduser(
    "~/Library/Application Support/JOAT/relationships.json",
)


def normalize_relationship(label: str) -> str:
    """"my wife" / "My Wife" / "wife" → "wife". Possessives are not identity."""
    words = [w for w in re.sub(r"[^a-z\s]", " ", label.casefold()).split() if w]
    while words and words[0] in ("my", "our", "the"):
        words.pop(0)
    return " ".join(words)


class RelationshipBook:
    """Who "my wife" is — told by the wearer, never inferred.

    The audit's M6: "send a message to my wife" had no way to resolve, and
    the contact resolver rightly refuses to guess a spouse from names or
    message frequency. This is the one place a relationship maps to a Mac
    contact name, written only by an explicit statement from the wearer
    ("Sam is my wife", through the remember_relationship tool) and stored
    as plain JSON under Application Support with owner-only permissions.
    Names only — no handles, so a stale entry can never redirect a send.
    """

    def __init__(self, path: str = RELATIONSHIPS_PATH) -> None:
        self._path = path
        self._entries: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
        if self._entries is None:
            try:
                with open(self._path, encoding="utf-8") as handle:
                    import json
                    raw = json.load(handle)
                self._entries = {
                    str(k): str(v) for k, v in raw.items()
                    if isinstance(k, str) and isinstance(v, str) and k and v
                } if isinstance(raw, dict) else {}
            except FileNotFoundError:
                self._entries = {}
            except Exception:
                log.warning("relationship book unreadable — starting empty")
                self._entries = {}
        return self._entries

    def resolve(self, label: str) -> str | None:
        """The contact name the wearer said this relationship is, if any."""
        key = normalize_relationship(label)
        return self._load().get(key) if key else None

    def remember(self, label: str, contact_name: str) -> str:
        """Record one relationship → contact name; returns the stored label."""
        key = normalize_relationship(label)
        name = " ".join(contact_name.split())
        if not key or not name:
            raise ValueError("relationship and contact name are both required")
        entries = self._load()
        entries[key] = name
        import json
        os.makedirs(os.path.dirname(self._path), mode=0o700, exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(entries, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)
        return key

    def forget(self, label: str) -> bool:
        key = normalize_relationship(label)
        entries = self._load()
        if key not in entries:
            return False
        del entries[key]
        import json
        with open(self._path, "w", encoding="utf-8") as handle:
            json.dump(entries, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.chmod(self._path, 0o600)
        return True

    def labels(self) -> dict[str, str]:
        return dict(self._load())
