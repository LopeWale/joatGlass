"""The environment the subscription CLIs are launched with (RULES.md #9, #10).

Every launch of the official ``claude`` and ``codex`` binaries runs on the
wearer's subscription login: the assistant seats, the Brainstorm seats, the
QuickNote completer and the phone's sign-in. A credential, provider switch
or endpoint the brain inherited from its shell would move a turn off that
login, onto a metered bill or another server, without anyone choosing it.
Every launch therefore drops the same list, and no path can narrow it.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

STRIP_ENV: tuple[str, ...] = (
    # Claude Code ranks each of these above a /login subscription
    # (code.claude.com/docs/en/authentication, "Authentication precedence"):
    # a cloud provider, a bearer token, an API key — always used under -p —
    # and an Anthropic profile or federation credential.
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    # Sends every request, the login with it, to another server.
    "ANTHROPIC_BASE_URL",
    # `codex exec` takes CODEX_API_KEY over the ChatGPT login ("API key via
    # env var takes precedence over any other auth method",
    # openai/codex codex-rs/login/src/auth/manager.rs), and OPENAI_API_KEY is
    # the key every OpenAI client reads.
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
)
"""Never inherited by any subscription CLI launch."""


def cli_env(extra: Iterable[str] = ()) -> dict[str, str]:
    """The brain's environment for one CLI launch: everything but
    `STRIP_ENV` and `extra`, with the CLIs' telemetry off."""
    dropped = frozenset((*STRIP_ENV, *extra))
    env = {key: value for key, value in os.environ.items() if key not in dropped}
    env["OTEL_SDK_DISABLED"] = "true"
    return env


__all__ = ["STRIP_ENV", "cli_env"]
