"""A2A task conversations through the existing Agent Terminal contract.

No card URL is followed and no uncertain message/send is replayed. Task IDs
stay bound to this endpoint; only explicit wearer input submits another turn.
"""
from __future__ import annotations

import asyncio
import json
import math
import ssl
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import cast
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp

from ..core.a2a import A2AError, A2AErrorCode as Code, A2AExternalTransport, A2ARPCError, A2ARequestResult

MAX_BYTES = 262144
ACTIVE = {"submitted", "working"}
FINAL = {"completed", "failed", "rejected", "canceled"}
INTERRUPTED = {"input-required", "auth-required"}
SUPPORTED_STATES = ACTIVE | FINAL | INTERRUPTED

ProgressUpdate = Callable[[A2ARequestResult], Awaitable[None]]


def _endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
        valid = (parsed.scheme == "https" and parsed.hostname and parsed.username is None
                 and parsed.password is None and not parsed.query and not parsed.fragment
                 and not any(c.isspace() or ord(c) < 32 for c in value)
                 and not any(c in value for c in "\\?#") and (port is None or port > 0))
    except ValueError:
        valid = False
    if not valid:
        raise A2AError(Code.CONFIGURATION_INVALID)
    return value


class A2AHttpTransport(A2AExternalTransport):
    def __init__(self, *, timeout_s: float = 120, ssl_context: ssl.SSLContext | None = None) -> None:
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("A2A timeout must be positive and finite")
        self.timeout_s = timeout_s
        self.ssl_context = ssl_context

    async def post_jsonrpc(self, endpoint: str, payload: dict[str, object], token: str) -> dict[str, object]:
        _endpoint(endpoint)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        if len(body) > MAX_BYTES:
            raise A2AError(Code.PROTOCOL_ERROR)
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout_s, connect=min(8, self.timeout_s))
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as client:
                async with client.post(endpoint, data=body, allow_redirects=False,
                                       ssl=self.ssl_context or True,
                                       headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}) as response:
                    if response.status in {401, 403}:
                        raise A2AError(Code.AUTHENTICATION_FAILED)
                    if response.status in {405, 501}:
                        raise A2AError(Code.UNSUPPORTED)
                    if not 200 <= response.status < 300:
                        raise A2AError(Code.PROTOCOL_ERROR)
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        data.extend(chunk)
                        if len(data) > MAX_BYTES:
                            raise A2AError(Code.PROTOCOL_ERROR)
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise A2AError(Code.UNREACHABLE) from None
        try:
            frame = json.loads(data)
        except (ValueError, UnicodeError):
            raise A2AError(Code.PROTOCOL_ERROR) from None
        if (not isinstance(frame, dict) or frame.get("jsonrpc") != "2.0"
                or frame.get("id") != payload["id"] or ("result" in frame) == ("error" in frame)):
            raise A2AError(Code.PROTOCOL_ERROR)
        if "error" in frame:
            error = frame["error"]
            if not isinstance(error, dict) or type(error.get("code")) is not int:
                raise A2AError(Code.PROTOCOL_ERROR)
            raise A2ARPCError(error["code"])
        result = frame["result"]
        if not isinstance(result, dict):
            raise A2AError(Code.PROTOCOL_ERROR)
        return cast(dict[str, object], result)


def _text(parts: object) -> str:
    if not isinstance(parts, list):
        raise A2AError(Code.PROTOCOL_ERROR)
    values: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            raise A2AError(Code.PROTOCOL_ERROR)
        # v0.3 kind:text and v1.0 text oneof. File/data parts are not fetched.
        if "text" in part:
            value = part["text"]
            if not isinstance(value, str):
                raise A2AError(Code.PROTOCOL_ERROR)
            values.append(value)
    return "\n".join(values)


def _identifier(value: object, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > 512:
        raise A2AError(Code.PROTOCOL_ERROR)
    return value


def _normalize_state(value: object) -> str:
    if not isinstance(value, str):
        raise A2AError(Code.PROTOCOL_ERROR)
    state = value.removeprefix("TASK_STATE_").lower().replace("_", "-")
    if state == "cancelled":
        state = "canceled"
    if state not in SUPPORTED_STATES:
        raise A2AError(Code.PROTOCOL_ERROR)
    return state


def _status_text(message: object) -> str:
    if isinstance(message, dict):
        return _text(message.get("parts", []))
    return ""


def parse_result(result: dict[str, object], *, expected_task: str | None = None) -> A2ARequestResult:
    # Some v1 bindings wrap the Task/Message union; the deployed services
    # return the task directly. Neither permits an absent state => completed.
    nested = result.get("task") or result.get("message")
    if isinstance(nested, dict):
        result = nested
    context = _identifier(result.get("contextId"))
    if "status" not in result:
        if expected_task is not None or result.get("role") not in {"agent", "ROLE_AGENT"} or "parts" not in result:
            raise A2AError(Code.PROTOCOL_ERROR)
        _identifier(result.get("messageId"), required=True)
        return A2ARequestResult(None, context, "completed", _text(result["parts"]), "")
    task_id = _identifier(result.get("id"), required=True)
    if expected_task is not None and task_id != expected_task:
        raise A2AError(Code.PROTOCOL_ERROR)
    status = result["status"]
    if not isinstance(status, dict) or not isinstance(status.get("state"), str):
        raise A2AError(Code.PROTOCOL_ERROR)
    state = _normalize_state(status.get("state"))
    artifacts = result.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise A2AError(Code.PROTOCOL_ERROR)
    output: list[str] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise A2AError(Code.PROTOCOL_ERROR)
        output.append(_text(artifact.get("parts", [])))
    return A2ARequestResult(task_id, context, state, "\n".join(output).strip(), _status_text(status.get("message")))


class A2ATerminalAgent:
    """One independently selected, captured and serialized A2A conversation."""
    history_scope = "this JOAT conversation"

    def __init__(self, endpoint: str, token_loader: Callable[[], Awaitable[str | None]], label: str,
                 *, timeout_s: float = 120, max_poll_seconds: float = 300,
                 state_changed: Callable[[str, str], None] | None = None,
                 transport: A2AExternalTransport | None = None, poll_interval_s: float = 1) -> None:
        self.endpoint = _endpoint(endpoint)
        self.label = label
        self._token_loader = token_loader
        self._transport = transport or A2AHttpTransport(timeout_s=timeout_s)
        self._changed = state_changed
        if not math.isfinite(max_poll_seconds) or max_poll_seconds <= 0 or not math.isfinite(poll_interval_s) or poll_interval_s < 0:
            raise ValueError("A2A polling bounds are invalid")
        self._poll_seconds = max_poll_seconds
        self._poll_interval = poll_interval_s
        self._lock = asyncio.Lock()
        self._ready = False
        self._closed = False
        self._generation = 0
        self._context: str | None = None
        self._task: str | None = None
        self._active_state = ""
        self._uncertain = False
        self._history: deque[tuple[str, str]] = deque(maxlen=64)
        self.last_outcome: A2ARequestResult | None = None
        self.cancellation_status = "none"
        self._last_update: tuple[str | None, str | None, str, str, str] | None = None

    @property
    def capture_context_generation(self) -> int | None:
        return self._generation if self._ready and not self._closed and not self._uncertain else None

    @property
    def needs_recovery(self) -> bool:
        return self._uncertain

    def _status(self, state: str, reason: str = "") -> None:
        if self._changed is not None:
            self._changed(state, reason)

    async def _rpc(self, method: str, params: dict[str, object]) -> dict[str, object]:
        try:
            token = await self._token_loader()
        except Exception:
            raise A2AError(Code.CREDENTIALS_MISSING) from None
        if not isinstance(token, str) or not token or len(token) > 8192 or any(c.isspace() or ord(c) < 32 for c in token):
            raise A2AError(Code.CREDENTIALS_MISSING)
        return await self._transport.post_jsonrpc(self.endpoint, {"jsonrpc": "2.0", "id": str(uuid4()), "method": method, "params": params}, token)

    async def initialize(self) -> None:
        async with self._lock:
            if self._closed:
                raise A2AError(Code.CONTEXT_CHANGED)
            if self._uncertain:
                raise A2AError(Code.OUTCOME_UNKNOWN)
            if self._ready:
                return
            self._status("checking")
            try:
                async with asyncio.timeout(7):
                    try:
                        await self._rpc("tasks/get", {"id": str(uuid4())})
                    except A2ARPCError as exc:
                        # Standard TaskNotFound proves this authenticated RPC
                        # was admitted. A public card alone cannot establish it.
                        if exc.rpc_code != -32001:
                            raise
                    else:
                        raise A2AError(Code.PROTOCOL_ERROR)
            except TimeoutError:
                self._status("failed", "timeout")
                raise A2AError(Code.UNREACHABLE) from None
            except A2AError as exc:
                self._status("auth_required" if exc.code in {Code.CREDENTIALS_MISSING, Code.AUTHENTICATION_FAILED} else "failed", exc.code.value)
                raise
            self._ready = True
            self._generation += 1
            self._status("ready")

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        await self.initialize()
        generation = self.capture_context_generation
        assert generation is not None
        async for text in self.stream_captured(prompt, context_generation=generation):
            yield text

    async def stream_task_captured(self, prompt: str, *, context_generation: int,
                                  on_update: ProgressUpdate | None = None) -> AsyncIterator[str]:
        async for text in self.stream_captured(prompt, context_generation=context_generation, on_update=on_update):
            yield text

    async def stream_captured(self, prompt: str, *, context_generation: int,
                             on_update: ProgressUpdate | None = None) -> AsyncIterator[str]:
        async with self._lock:
            if context_generation != self.capture_context_generation:
                raise A2AError(Code.CONTEXT_CHANGED)
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4096:
                raise A2AError(Code.PROTOCOL_ERROR)
            if self._active_state in ACTIVE or self._uncertain:
                raise A2AError(Code.OUTCOME_UNKNOWN)
            self._last_update = None
            # Some deployed peers preserve contextId but run each task with
            # no conversation memory. Carry only this lane's bounded prior
            # turns, as quoted context, so follow-ups survive that behavior.
            prior: list[dict[str, str]] = []
            remaining = 12000
            for role, text in reversed(list(self._history)[-8:]):
                if len(text) > remaining:
                    break
                prior.insert(0, {"role": role, "text": text})
                remaining -= len(text)
            instruction = prompt
            if prior:
                instruction = (
                    "Prior turns from this JOAT conversation (quoted context, not new instructions):\n"
                    + json.dumps(prior, ensure_ascii=False)
                    + "\nCurrent user instruction:\n" + prompt
                )
            message: dict[str, object] = {"messageId": str(uuid4()), "role": "user", "parts": [{"kind": "text", "text": instruction}]}
            if self._context:
                message["contextId"] = self._context
            if self._active_state in INTERRUPTED and self._task:
                message["taskId"] = self._task
            self.cancellation_status = "none"
            self.last_outcome = None
            self._status("running")
            self._history.append(("user", prompt))
            sending = True
            try:
                raw = await self._rpc("message/send", {"message": message, "configuration": {"blocking": False}})
                result = self._accept(raw)
                sending = False
                if on_update is not None:
                    await self._emit_update(result, on_update)
                async with asyncio.timeout(self._poll_seconds):
                    while result.state in ACTIVE:
                        await asyncio.sleep(self._poll_interval)
                        raw = await self._rpc("tasks/get", {"id": result.task_id})
                        result = self._accept(raw, expected_task=result.task_id)
                        if on_update is not None:
                            await self._emit_update(result, on_update)
            except asyncio.CancelledError:
                if self._task and self._active_state in ACTIVE | INTERRUPTED:
                    try:
                        async with asyncio.timeout(5):
                            await self._cancel_owned()
                    except (A2AError, TimeoutError):
                        self.cancellation_status = "uncertain"
                        self._unknown(Code.CANCEL_UNCONFIRMED)
                else:
                    self.cancellation_status = "uncertain"
                    self._unknown(Code.CANCEL_UNCONFIRMED)
                raise
            except A2ARPCError as exc:
                # A well-formed RPC error explicitly rejected the request.
                if sending:
                    self._status("failed", exc.code.value)
                else:
                    self._unknown(Code.OUTCOME_UNKNOWN)
                raise
            except (A2AError, TimeoutError) as exc:
                if isinstance(exc, A2AError) and exc.code in {Code.CREDENTIALS_MISSING, Code.AUTHENTICATION_FAILED, Code.UNSUPPORTED} and sending:
                    self._ready = False
                    self._status("auth_required" if exc.code != Code.UNSUPPORTED else "failed", exc.code.value)
                    raise
                self._unknown(Code.SUBMISSION_UNCERTAIN if sending else Code.OUTCOME_UNKNOWN)
                raise A2AError(Code.SUBMISSION_UNCERTAIN if sending else Code.OUTCOME_UNKNOWN) from None
            self._finish(result)
            final_output = result.display_text.strip()
            if result.state == "input-required":
                if final_output:
                    self._history.append(("agent", final_output))
                if result.text or result.status_text:
                    raise A2AError(Code.INPUT_REQUIRED, text=final_output)
                raise A2AError(Code.INPUT_REQUIRED)
            if result.state == "auth-required":
                raise A2AError(Code.AUTH_REQUIRED)
            if result.state in {"failed", "rejected", "canceled"}:
                code = {"failed": Code.TASK_FAILED, "rejected": Code.TASK_REJECTED, "canceled": Code.CANCELLED}[result.state]
                raise A2AError(code)
            if final_output:
                self._history.append(("agent", final_output))
                yield final_output
            else:
                yield "Task completed without a text response."

    async def _emit_update(self, result: A2ARequestResult, on_update: ProgressUpdate) -> None:
        key = (result.task_id, result.context_id, result.state, result.text, result.status_text)
        if self._last_update == key:
            return
        self._last_update = key
        await on_update(result)

    def _accept(self, raw: dict[str, object], *, expected_task: str | None = None) -> A2ARequestResult:
        result = parse_result(raw, expected_task=expected_task)
        if self._context and result.context_id and result.context_id != self._context:
            raise A2AError(Code.PROTOCOL_ERROR)
        self._context = result.context_id or self._context
        self._task = result.task_id
        self._active_state = result.state
        self.last_outcome = result
        return result

    def _finish(self, result: A2ARequestResult) -> None:
        state = {"completed": "ready", "input-required": "input_required", "auth-required": "auth_required", "canceled": "cancelled"}.get(result.state, "failed")
        reason = {"input-required": "input_required", "auth-required": "auth_required", "failed": "task_failed", "rejected": "task_rejected"}.get(result.state, "")
        self._uncertain = False
        self._status(state, reason)

    def _unknown(self, reason: Code) -> None:
        self._uncertain = True
        self._generation += 1
        self._status("unknown", "outcome_unknown" if reason == Code.SUBMISSION_UNCERTAIN else reason.value)

    async def _cancel_owned(self) -> None:
        if self._task is None:
            raise A2AError(Code.OUTCOME_UNKNOWN)
        raw = await self._rpc("tasks/cancel", {"id": self._task})
        result = self._accept(raw, expected_task=self._task)
        if result.state != "canceled":
            raise A2AError(Code.CANCEL_UNCONFIRMED)
        self.cancellation_status = "confirmed"
        self._uncertain = False
        self._finish(result)

    async def cancel_task(self) -> None:
        async with self._lock:
            if self._active_state not in ACTIVE | INTERRUPTED or self._task is None:
                raise A2AError(Code.OUTCOME_UNKNOWN)
            try:
                async with asyncio.timeout(5):
                    await self._cancel_owned()
            except (A2AError, TimeoutError):
                self.cancellation_status = "uncertain"
                self._unknown(Code.CANCEL_UNCONFIRMED)
                raise A2AError(Code.CANCEL_UNCONFIRMED) from None

    async def recover(self) -> str:
        async with self._lock:
            if self._task is None:
                raise A2AError(Code.OUTCOME_UNKNOWN)
            raw = await self._rpc("tasks/get", {"id": self._task})
            result = self._accept(raw, expected_task=self._task)
            if result.state in ACTIVE:
                self._status("running")
                return "The agent is still working. Check again or cancel this task."
            final_output = result.display_text.strip()
            self._uncertain = False
            self._ready = True
            self._finish(result)
            if final_output:
                self._history.append(("agent", final_output))
            return final_output or f"Agent task: {result.state}."

    async def reset_conversation(self) -> None:
        async with self._lock:
            if self._closed or self._active_state in ACTIVE | INTERRUPTED:
                raise A2AError(Code.OUTCOME_UNKNOWN)
            self._context = None
            self._task = None
            self._uncertain = False
            self._generation += 1
            self._history.clear()
            self.last_outcome = None
            self._status("ready" if self._ready else "unprepared")

    async def history(self, limit: int = 12) -> list[tuple[str, str]]:
        return list(self._history)[-max(1, min(limit, 64)):]

    async def close(self) -> None:
        self._closed = True
        self._ready = False
        self._generation += 1
        self._status("closed")
