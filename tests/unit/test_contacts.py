"""Mac-local contact search for explicitly confirmed new Messages."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from joat.channels import contacts


def _contacts_db(path: Path) -> Path:
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE ZABCDRECORD (
            Z_PK INTEGER, ZFIRSTNAME TEXT, ZLASTNAME TEXT, ZORGANIZATION TEXT
        );
        CREATE TABLE ZABCDPHONENUMBER (ZFULLNUMBER TEXT, ZOWNER INTEGER);
        CREATE TABLE ZABCDEMAILADDRESS (ZADDRESS TEXT, ZOWNER INTEGER);
        INSERT INTO ZABCDRECORD VALUES (1, 'Adele', 'Vance', '');
        INSERT INTO ZABCDRECORD VALUES (2, 'Adele', 'Okafor', '');
        INSERT INTO ZABCDRECORD VALUES (3, 'Dele', 'Akin', '');
        INSERT INTO ZABCDPHONENUMBER VALUES ('+1 (405) 555-0177', 1);
        INSERT INTO ZABCDEMAILADDRESS VALUES ('adele.okafor@example.com', 2);
        INSERT INTO ZABCDPHONENUMBER VALUES ('+1 405 555 0199', 3);
    """)
    connection.commit()
    connection.close()
    return path


def test_contact_search_returns_mac_owned_candidates_without_guessing(
    tmp_path: Path, monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(contacts, "_SOURCES", (str(_contacts_db(tmp_path / "contacts.db")),))
    resolver = contacts.ContactResolver()

    assert resolver.search("adele") == [
        ("Adele Okafor", "adele.okafor@example.com"),
        ("Adele Vance", "+1 (405) 555-0177"),
    ]
    assert resolver.search("Adele Vance") == [
        ("Adele Vance", "+1 (405) 555-0177"),
    ]
    assert resolver.search("unknown") == []
