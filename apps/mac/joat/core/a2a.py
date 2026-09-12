"""Typed boundary for authenticated A2A task transports."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum

MAX_BYTES = 262144


class A2AErrorCode(StrEnum):
    CREDENTIALS_MISSING = "credentials_missing"
    AUTHENTICATION_FAILED = "authentication_failed"
    AUTH_REQUIRED = "auth_required"
    INPUT_REQUIRED = "input_required"
    OUTCOME_UNKNOWN = "outcome_unknown"
    SUBMISSION_UNCERTAIN = "submission_uncertain"
    TASK_FAILED = "task_failed"
    TASK_REJECTED = "task_rejected"
    CANCELLED = "cancelled"
    CANCEL_UNCONFIRMED = "cancel_unconfirmed"
    PROTOCOL_ERROR = "protocol_error"
    UNREACHABLE = "unreachable"
    UNSUPPORTED = "unsupported"
    CONTEXT_CHANGED = "context_changed"
    CONFIGURATION_INVALID = "configuration_invalid"


_MESSAGES = {
    A2AErrorCode.CREDENTIALS_MISSING: "Save this agent's token in JOAT on the Mac, then retry.",
    A2AErrorCode.AUTHENTICATION_FAILED: "The agent rejected its token. Update it on the Mac, then retry.",
    A2AErrorCode.AUTH_REQUIRED: "The agent needs additional authorization. Check its dashboard.",
    A2AErrorCode.INPUT_REQUIRED: "The agent is waiting for your next instruction.",
    A2AErrorCode.OUTCOME_UNKNOWN: "Task outcome unknown. Recover its status before sending again.",
    A2AErrorCode.SUBMISSION_UNCERTAIN: "The send outcome is unknown. It has not been sent again.",
    A2AErrorCode.TASK_FAILED: "The agent reported that this task failed.",
    A2AErrorCode.TASK_REJECTED: "The agent declined this task.",
    A2AErrorCode.CANCELLED: "The agent cancelled this task.",
    A2AErrorCode.CANCEL_UNCONFIRMED: "Remote cancellation is unconfirmed. Check the agent.",
    A2AErrorCode.PROTOCOL_ERROR: "The agent returned an invalid response. Check its connection.",
    A2AErrorCode.UNREACHABLE: "Could not reach the agent. Check Tailscale and its endpoint.",
    A2AErrorCode.UNSUPPORTED: "The agent does not support this operation.",
    A2AErrorCode.CONTEXT_CHANGED: "The selected conversation changed. Open it again before sending.",
    A2AErrorCode.CONFIGURATION_INVALID: "The agent endpoint is invalid. Correct it on the Mac.",
}


class A2AError(RuntimeError):
    """Only fixed safe copy crosses into the terminal and logs."""
    def __init__(self, code: A2AErrorCode, *, text: str = "") -> None:
        super().__init__(_MESSAGES[code])
        self.code = code
        # An input-required question is response content, never exception text.
        self.text = text


class A2ARPCError(A2AError):
    def __init__(self, rpc_code: int) -> None:
        super().__init__(A2AErrorCode.UNSUPPORTED if rpc_code == -32601 else A2AErrorCode.TASK_REJECTED)
        self.rpc_code = rpc_code


class A2AExternalTransport(ABC):
    @abstractmethod
    async def post_jsonrpc(self, endpoint: str, payload: dict[str, object], token: str) -> dict[str, object]: ...

    def post_jsonrpc_events(self, endpoint: str, payload: dict[str, object], token: str) -> AsyncIterator[dict[str, object]]:  # pragma: no cover - default disabled in test doubles
        raise A2AError(A2AErrorCode.UNSUPPORTED)


@dataclass(frozen=True, slots=True)
class A2ARequestResult:
    task_id: str | None
    context_id: str | None
    state: str
    text: str
    status_text: str = ""

    @property
    def requires_input(self) -> bool:
        return self.state == "input-required"

    @property
    def final(self) -> bool:
        return self.state in {"completed", "failed", "rejected", "canceled"}

    @property
    def display_text(self) -> str:
        if self.state == "input-required" and self.status_text:
            return self.status_text
        return self.text or self.status_text
