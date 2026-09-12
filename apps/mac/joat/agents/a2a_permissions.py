"""Operator-selected A2A provider policy, separate from untrusted task prose.

A2A authentication and remote tool approval are different authorities. Neither
an auth-required state nor a model's request to "approve" grants JOAT an RPC
that the provider has not implemented and negotiated.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class A2AProvider(StrEnum):
    OPENCLAW = "openclaw"
    HERMES = "hermes"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProviderPermissionPolicy:
    provider: A2AProvider
    display_name: str
    permission_guidance: str
    authentication_guidance: str
    credential_guidance: str
    tool_policy: Literal["remote_managed"] = "remote_managed"
    # No supported provider approval extension is implemented in JOAT.
    approval_rpc: None = None
    verified_no_tools: Literal[False] = False
    can_approve: Literal[False] = False
    can_deny: Literal[False] = False


@dataclass(frozen=True, slots=True)
class PermissionNotice:
    kind: Literal["authorization_required"]
    text: str
    can_approve: Literal[False] = False
    can_deny: Literal[False] = False


def provider_permissions(provider: A2AProvider | str | None = None) -> ProviderPermissionPolicy:
    """Use an explicit local provider setting, never endpoint/card/text guesses.

    A profile ID may serve as the setting only if the local configuration
    defines that meaning. Unknown names retain the conservative generic policy.
    These descriptors explain supported integrations; they do not attest to
    the installed remote version or remotely configured tool permissions.
    """
    if provider == A2AProvider.OPENCLAW:
        return ProviderPermissionPolicy(
            A2AProvider.OPENCLAW,
            "OpenClaw",
            "Tools and permissions are managed in OpenClaw. If a tool needs approval, "
            "use OpenClaw's Control UI or an authorized user channel; A2A cannot approve it.",
            "OpenClaw requires additional authorization for this task. Review it "
            "in OpenClaw, then check the task again. JOAT cannot approve tools from this status.",
            "Check the saved A2A peer credential in JOAT on your Mac "
            "and the peer's access in OpenClaw.",
        )
    if provider == A2AProvider.HERMES:
        return ProviderPermissionPolicy(
            A2AProvider.HERMES,
            "Hermes",
            "Tools and permissions are managed in Hermes. Review them in Hermes; "
            "JOAT has no negotiated A2A tool approval control for this connection.",
            "Hermes requires additional authorization for this task. Review it "
            "in Hermes, then check the task again. JOAT cannot approve tools from this status.",
            "Check the saved A2A credential in JOAT on your Mac "
            "and the authenticated peer's access in Hermes.",
        )
    return ProviderPermissionPolicy(
        A2AProvider.UNKNOWN,
        "Remote agent",
        "Tools and permissions are managed by the remote agent. "
        "No A2A tool approval control has been negotiated for this connection.",
        "The remote agent requires additional authorization for this task. Review "
        "the task with that agent, then check it again. "
        "JOAT cannot approve tools from this status.",
        "Check the remote agent's access settings and the saved A2A credential "
        "in JOAT on your Mac.",
    )


def permission_notice(
    provider: A2AProvider | str | None,
    task_state: object,
) -> PermissionNotice | None:
    """Render exact wire authentication states, never infer approval from prose.

    Working does not prove an approval is pending. Input-required requests
    conversational input, not authorization to run a tool. Unknown extension
    data cannot manufacture an approval capability through this helper.
    """
    if not isinstance(task_state, str) or task_state not in {
        "auth-required", "TASK_STATE_AUTH_REQUIRED",
    }:
        return None
    return PermissionNotice(
        "authorization_required", provider_permissions(provider).authentication_guidance,
    )
