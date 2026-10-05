"""Saved subscription accounts for the two CLI seats, switchable from the phone.

Each account is a separate home directory for the official CLI, which is
how both vendors document running more than one login side by side:

- Claude Code: ``CLAUDE_CONFIG_DIR``. "Each directory has its own settings,
  session history, and claude.ai login", and on macOS the Keychain entry is
  keyed to that directory too (code.claude.com/docs/en/authentication,
  "Log in with multiple accounts" and "Credential management").
- Codex CLI: ``CODEX_HOME`` — credentials are cached in
  ``$CODEX_HOME/auth.json`` (learn.chatgpt.com/docs/auth).

JOAT never reads, copies or forwards a credential (RULES.md #9, #10): it
only chooses WHICH directory the official binary is started with. The
"default" account is the CLI's own home — no variable at all — so whatever
the wearer already signed into in Terminal keeps working unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

Provider = Literal["claude", "codex"]
PROVIDERS: tuple[Provider, ...] = ("claude", "codex")

HOME_ENV: dict[Provider, str] = {
    "claude": "CLAUDE_CONFIG_DIR",
    "codex": "CODEX_HOME",
}

PROVIDER_TITLES: dict[Provider, str] = {"claude": "Claude", "codex": "Codex"}

DEFAULT_PROFILE_ID = "default"
DEFAULT_ACCOUNTS_ROOT = Path.home() / "Library/Application Support/JOAT/accounts"
MAX_LABEL = 40
MAX_PROFILES = 8

_PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


@dataclass(frozen=True)
class AccountProfile:
    """One saved login for one CLI."""

    provider: Provider
    profile_id: str
    label: str
    home: str | None
    """The CLI home directory, or None for the CLI's own default home."""

    @property
    def is_default(self) -> bool:
        return self.home is None


@dataclass
class _ProviderBook:
    active: str = DEFAULT_PROFILE_ID
    profiles: list[AccountProfile] = field(default_factory=list)


class AccountError(ValueError):
    """A request the registry refuses; the message is safe to show the wearer."""


def clean_label(label: str) -> str:
    """A wearer-typed account name, single line and bounded."""
    text = " ".join(str(label).split())
    if not text:
        raise AccountError("Give the account a name.")
    return text[:MAX_LABEL]


class AccountRegistry:
    """Which accounts exist per CLI, and which one each seat runs as.

    Persisted to ``accounts.json`` beside the account homes, written
    atomically and owner-only. Holds names and directories — never tokens.
    """

    def __init__(self, root: Path = DEFAULT_ACCOUNTS_ROOT) -> None:
        self._root = root
        self._file = root / "accounts.json"
        self._books: dict[Provider, _ProviderBook] = {p: _ProviderBook() for p in PROVIDERS}
        self._listeners: list[Callable[[], None]] = []
        self._load()

    # ── reading ─────────────────────────────────────────────────────────────

    def profiles(self, provider: Provider) -> list[AccountProfile]:
        return list(self._books[provider].profiles)

    def active(self, provider: Provider) -> AccountProfile:
        book = self._books[provider]
        for profile in book.profiles:
            if profile.profile_id == book.active:
                return profile
        return book.profiles[0]

    def get(self, provider: Provider, profile_id: str) -> AccountProfile:
        for profile in self._books[provider].profiles:
            if profile.profile_id == profile_id:
                return profile
        raise AccountError("That account no longer exists.")

    def env_overlay(self, provider: Provider, profile: AccountProfile | None = None) -> dict[str, str | None]:
        """The environment a spawn needs to run as this account.

        None removes the variable, so "default" is the CLI's real default
        even when the brain inherited a stray CLAUDE_CONFIG_DIR.
        """
        chosen = profile if profile is not None else self.active(provider)
        return {HOME_ENV[provider]: chosen.home}

    def env_provider(self, provider: Provider) -> Callable[[], Mapping[str, str | None]]:
        """A late-bound reader for seats constructed once at boot."""
        return lambda: self.env_overlay(provider)

    def on_change(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    # ── changing ────────────────────────────────────────────────────────────

    def add(self, provider: Provider, label: str) -> AccountProfile:
        book = self._books[provider]
        if len(book.profiles) >= MAX_PROFILES:
            raise AccountError(f"{PROVIDER_TITLES[provider]} already has {MAX_PROFILES} accounts.")
        name = clean_label(label)
        profile_id = f"acct-{secrets.token_hex(4)}"
        home = self._root / provider / profile_id
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(home, 0o700)
        profile = AccountProfile(provider, profile_id, name, str(home))
        book.profiles.append(profile)
        self._save()
        return profile

    def rename(self, provider: Provider, profile_id: str, label: str) -> AccountProfile:
        book = self._books[provider]
        old = self.get(provider, profile_id)
        new = AccountProfile(provider, profile_id, clean_label(label), old.home)
        book.profiles = [new if p.profile_id == profile_id else p for p in book.profiles]
        self._save()
        return new

    def activate(self, provider: Provider, profile_id: str) -> AccountProfile:
        profile = self.get(provider, profile_id)
        self._books[provider].active = profile_id
        self._save()
        return profile

    def remove(self, provider: Provider, profile_id: str) -> AccountProfile:
        """Forget an account. The caller signs it out FIRST, so the CLI
        revokes its own credential before the directory is forgotten."""
        profile = self.get(provider, profile_id)
        if profile.is_default:
            raise AccountError("The default account can be signed out, not removed.")
        book = self._books[provider]
        book.profiles = [p for p in book.profiles if p.profile_id != profile_id]
        if book.active == profile_id:
            book.active = DEFAULT_PROFILE_ID
        self._save()
        return profile

    # ── persistence ─────────────────────────────────────────────────────────

    def _default(self, provider: Provider) -> AccountProfile:
        return AccountProfile(provider, DEFAULT_PROFILE_ID, "This Mac's login", None)

    def _load(self) -> None:
        raw: object = {}
        try:
            raw = json.loads(self._file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError):
            log.warning("accounts.json unreadable — starting from the default accounts")
            raw = {}
        data = raw if isinstance(raw, dict) else {}
        for provider in PROVIDERS:
            entry = data.get(provider)
            book = _ProviderBook(profiles=[self._default(provider)])
            if isinstance(entry, dict):
                for item in entry.get("profiles", []):
                    profile = self._parse_profile(provider, item)
                    if profile is not None and not profile.is_default:
                        book.profiles.append(profile)
                active = entry.get("active")
                if isinstance(active, str) and any(p.profile_id == active for p in book.profiles):
                    book.active = active
            self._books[provider] = book

    def _parse_profile(self, provider: Provider, item: object) -> AccountProfile | None:
        if not isinstance(item, dict):
            return None
        profile_id = item.get("profile_id")
        label = item.get("label")
        home = item.get("home")
        if not isinstance(profile_id, str) or not _PROFILE_ID.match(profile_id):
            return None
        if profile_id == DEFAULT_PROFILE_ID or not isinstance(label, str):
            return None
        expected = self._root / provider / profile_id
        # Only a home JOAT itself created: a hand-edited file must not point
        # a seat at an arbitrary directory.
        if not isinstance(home, str) or Path(home) != expected:
            return None
        try:
            name = clean_label(label)
        except AccountError:
            return None
        return AccountProfile(provider, profile_id, name, home)

    def _save(self) -> None:
        payload = {
            provider: {
                "active": self._books[provider].active,
                "profiles": [
                    {k: v for k, v in asdict(p).items() if k != "provider"}
                    for p in self._books[provider].profiles if not p.is_default
                ],
            }
            for provider in PROVIDERS
        }
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self._file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(tmp, self._file)
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
                log.warning("account listener failed", exc_info=False)


def parse_provider(value: object) -> Provider:
    if value == "claude":
        return "claude"
    if value == "codex":
        return "codex"
    raise AccountError("Unknown account type.")


__all__ = [
    "DEFAULT_ACCOUNTS_ROOT",
    "DEFAULT_PROFILE_ID",
    "HOME_ENV",
    "PROVIDERS",
    "PROVIDER_TITLES",
    "AccountError",
    "AccountProfile",
    "AccountRegistry",
    "Provider",
    "clean_label",
    "parse_provider",
]
