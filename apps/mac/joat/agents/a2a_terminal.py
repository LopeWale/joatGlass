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
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp

from ..core.a2a import (MAX_BYTES, A2AError, A2AErrorCode as Code,
                        A2AExternalTransport, A2ARPCError, A2ARequestResult)
from .a2a_stream import iter_sse_data, parse_sse_payload
from .a2a_permissions import provider_permissions
ACTIVE = {"submitted", "working"}
FINAL = {"completed", "failed", "rejected", "canceled"}
INTERRUPTED = {"input-required", "auth-required"}
SUPPORTED_STATES = ACTIVE | FINAL | INTERRUPTED
_DISPATCH_PHASES = {
    "", "sending", "accepted", "settled", "uncertain", "reset", "closed", "cancel_pending",
    "pre_send", "send_accepted", "poll_update", "stream_update", "emit_update", "recover", "result", "checkpoint_error",
}
_CHECKPOINT_PHASES = {"", "pre_send", "send_accepted", "poll_update", "stream_update", "emit_update", "recover", "result", "checkpoint_error", "cancel_pending", "cancel_result"}

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
        body = self._encode_payload(payload)
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout_s, connect=min(8, self.timeout_s))
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as client:
                async with client.post(
                    endpoint,
                    data=body,
                    allow_redirects=False,
                    ssl=self.ssl_context or True,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                ) as response:
                    self._validate_response(response)
                    frame = await self._read_json_frame(response)
            return self._parse_frame(frame, payload)
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise A2AError(Code.UNREACHABLE) from None

    def post_jsonrpc_events(self, endpoint: str, payload: dict[str, object], token: str) -> AsyncIterator[dict[str, object]]:
        async def _events() -> AsyncIterator[dict[str, object]]:
            try:
                body = self._encode_payload(payload)
                timeout = aiohttp.ClientTimeout(total=self.timeout_s, connect=min(8, self.timeout_s))
                async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as client:
                    async with client.post(
                        endpoint,
                        data=body,
                        allow_redirects=False,
                        ssl=self.ssl_context or True,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/json",
                            "Accept": "text/event-stream, application/json",
                        },
                    ) as response:
                        self._validate_response(response)
                        content_type = response.headers.get("Content-Type", "").lower()
                        if "text/event-stream" in content_type:
                            async for raw in iter_sse_data(response.content.iter_chunked(16384), max_bytes=MAX_BYTES):
                                event = parse_sse_payload(raw)
                                yield self._parse_frame(event, payload)
                        else:
                            event = await self._read_json_frame(response)
                            yield self._parse_frame(event, payload)
            except (aiohttp.ClientError, TimeoutError, OSError):
                raise A2AError(Code.UNREACHABLE) from None

        return _events()

    async def _read_json_frame(self, response: aiohttp.ClientResponse) -> dict[str, object]:
        data = bytearray()
        async for chunk in response.content.iter_chunked(16384):
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                raise A2AError(Code.PROTOCOL_ERROR)
        if not data:
            raise A2AError(Code.PROTOCOL_ERROR)
        try:
            frame = json.loads(data)
        except (ValueError, UnicodeError) as exc:
            raise A2AError(Code.PROTOCOL_ERROR) from exc
        if not isinstance(frame, dict):
            raise A2AError(Code.PROTOCOL_ERROR)
        return cast(dict[str, object], frame)

    def _encode_payload(self, payload: dict[str, object]) -> bytes:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        if len(body) > MAX_BYTES:
            raise A2AError(Code.PROTOCOL_ERROR)
        return body

    @staticmethod
    def _validate_response(response: aiohttp.ClientResponse) -> None:
        if response.status in {401, 403}:
            raise A2AError(Code.AUTHENTICATION_FAILED)
        if response.status in {405, 501}:
            raise A2AError(Code.UNSUPPORTED)
        if not 200 <= response.status < 300:
            raise A2AError(Code.PROTOCOL_ERROR)

    def _parse_frame(self, frame: dict[str, object], request_payload: dict[str, object]) -> dict[str, object]:
        if (not isinstance(frame, dict) or frame.get("jsonrpc") != "2.0"
                or frame.get("id") != request_payload["id"] or ("result" in frame) == ("error" in frame)):
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
    return "".join(values)


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
                 transport: A2AExternalTransport | None = None, poll_interval_s: float = 1,
                 streaming: bool = False, provider: str = "generic",
                 checkpoint: Callable[[dict[str, object]], Awaitable[None]] | None = None) -> None:
        self.endpoint = _endpoint(endpoint)
        self.label = label
        self._token_loader = token_loader
        self._transport = transport or A2AHttpTransport(timeout_s=timeout_s)
        self._changed = state_changed
        self._checkpoint = checkpoint
        self._streaming = streaming
        self._provider = provider
        if not math.isfinite(max_poll_seconds) or max_poll_seconds <= 0 or not math.isfinite(poll_interval_s) or poll_interval_s < 0:
            raise ValueError("A2A polling bounds are invalid")
        self._poll_seconds = max_poll_seconds
        self._poll_interval = poll_interval_s
        self._lock = asyncio.Lock()
        self._ready = False
        self._closed = False
        self._detached_for_shutdown = False
        self._generation = 0
        self._context: str | None = None
        self._task: str | None = None
        self._dispatch_id: str | None = None
        self._dispatch_phase = ""
        self._checkpoint_phase = ""
        self._active_state = ""
        self._uncertain = False
        self._history: deque[tuple[str, str]] = deque(maxlen=64)
        self.last_outcome: A2ARequestResult | None = None
        self.cancellation_status = "none"
        self._last_update: tuple[str | None, str | None, str, str, str] | None = None

    @property
    def capture_context_generation(self) -> int | None:
        return self._generation if self._ready and not self._closed and not self.needs_recovery else None

    @property
    def needs_recovery(self) -> bool:
        return self._uncertain or self._active_state == "auth-required"

    @property
    def streaming_enabled(self) -> bool:
        return self._streaming

    @property
    def provider(self) -> str:
        return self._provider

    def set_checkpoint(self, callback: Callable[[dict[str, object]], Awaitable[None]] | None) -> None:
        self._checkpoint = callback

    def _encode_history(self) -> list[list[str]]:
        history = [list(item) for item in self._history]
        if not history:
            return history
        payload = {"history": history}
        while len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_BYTES:
            if not history:
                break
            history.pop(0)
        return history

    def export_state(self) -> dict[str, object]:
        state: dict[str, object] = {
            "context": self._context,
            "task": self._task,
            "dispatch_id": self._dispatch_id,
            "dispatch_phase": self._dispatch_phase,
            "checkpoint_phase": self._checkpoint_phase,
            "active_state": self._active_state,
            "ready": self._ready,
            "closed": self._closed,
            "uncertain": self._uncertain,
            "history": self._encode_history(),
        }
        if self.last_outcome is not None:
            state["last_outcome"] = {
                "task_id": self.last_outcome.task_id,
                "context_id": self.last_outcome.context_id,
                "state": self.last_outcome.state,
                "text": self.last_outcome.text,
                "status_text": self.last_outcome.status_text,
            }
        else:
            state["last_outcome"] = None
        state["generation"] = self._generation
        state["detached_for_shutdown"] = self._detached_for_shutdown
        return state

    def restore_state(self, snapshot: Mapping[str, object]) -> None:
        if not isinstance(snapshot, Mapping):
            raise A2AError(Code.PROTOCOL_ERROR)

        context = _identifier(snapshot.get("context"))
        task = _identifier(snapshot.get("task"))
        if task is None and snapshot.get("task") is not None:
            raise A2AError(Code.PROTOCOL_ERROR)
        active_state = snapshot.get("active_state")
        if active_state is not None and not isinstance(active_state, str):
            raise A2AError(Code.PROTOCOL_ERROR)
        if active_state not in (None, "",) and active_state not in SUPPORTED_STATES:
            raise A2AError(Code.PROTOCOL_ERROR)
        dispatch_id = _identifier(snapshot.get("dispatch_id"), required=False)
        dispatch_phase = snapshot.get("dispatch_phase")
        if dispatch_phase is not None and not isinstance(dispatch_phase, str):
            raise A2AError(Code.PROTOCOL_ERROR)
        if dispatch_phase is not None and dispatch_phase not in _DISPATCH_PHASES:
            raise A2AError(Code.PROTOCOL_ERROR)
        checkpoint_phase = snapshot.get("checkpoint_phase")
        if checkpoint_phase is not None and not isinstance(checkpoint_phase, str):
            raise A2AError(Code.PROTOCOL_ERROR)
        if checkpoint_phase is not None and checkpoint_phase not in _CHECKPOINT_PHASES:
            raise A2AError(Code.PROTOCOL_ERROR)
        uncertain = snapshot.get("uncertain")
        if uncertain is not None and not isinstance(uncertain, bool):
            raise A2AError(Code.PROTOCOL_ERROR)
        detached = snapshot.get("detached_for_shutdown")
        if detached is not None and not isinstance(detached, bool):
            raise A2AError(Code.PROTOCOL_ERROR)
        closed = snapshot.get("closed")
        if closed is not None and not isinstance(closed, bool):
            raise A2AError(Code.PROTOCOL_ERROR)
        generation = snapshot.get("generation")
        if generation is not None and (not isinstance(generation, int) or generation < 0):
            raise A2AError(Code.PROTOCOL_ERROR)

        history_items = snapshot.get("history")
        if history_items is not None and not isinstance(history_items, list):
            raise A2AError(Code.PROTOCOL_ERROR)
        history: deque[tuple[str, str]] = deque(maxlen=64)
        if history_items is not None:
            for item in history_items:
                if not isinstance(item, list) or len(item) != 2:
                    raise A2AError(Code.PROTOCOL_ERROR)
                role, text = item
                if not isinstance(role, str) or not isinstance(text, str):
                    raise A2AError(Code.PROTOCOL_ERROR)
                if role not in {"user", "agent"} or len(text.encode("utf-8")) > MAX_BYTES:
                    raise A2AError(Code.PROTOCOL_ERROR)
                history.append((role, text))
        if history_items is not None and (len(history_items) > 64 or len(json.dumps({"history": history_items}, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_BYTES):
            raise A2AError(Code.PROTOCOL_ERROR)

        restored_outcome = snapshot.get("last_outcome")
        if isinstance(restored_outcome, dict):
            task_id = _identifier(restored_outcome.get("task_id")) if restored_outcome.get("task_id") is not None else None
            context_id = _identifier(restored_outcome.get("context_id")) if restored_outcome.get("context_id") is not None else None
            text = restored_outcome.get("text")
            status_text = restored_outcome.get("status_text")
            state_value = restored_outcome.get("state")
            if not isinstance(state_value, str) or state_value not in SUPPORTED_STATES:
                raise A2AError(Code.PROTOCOL_ERROR)
            if text is not None and (not isinstance(text, str) or len(text) > 262144):
                raise A2AError(Code.PROTOCOL_ERROR)
            if status_text is not None and (not isinstance(status_text, str) or len(status_text) > 262144):
                raise A2AError(Code.PROTOCOL_ERROR)
            result = A2ARequestResult(task_id=task_id, context_id=context_id, state=state_value, text=text or "", status_text=status_text or "")
        elif restored_outcome is None:
            result = None
        else:
            raise A2AError(Code.PROTOCOL_ERROR)

        recovered_task = task
        restored_state = str(active_state or "")
        restored_dispatch_phase = str(dispatch_phase or "")
        restored_uncertain = bool(uncertain) or restored_state in ACTIVE or restored_state == "auth-required"
        if restored_dispatch_phase in {"sending", "pre_send"}:
            # Even continuation may have reached the provider with no receipt.
            restored_uncertain = True
            if restored_state not in INTERRUPTED:
                recovered_task = None
                restored_state = ""
        elif result is not None and result.state in FINAL | {"input-required"}:
            restored_uncertain = False
            restored_dispatch_phase = "settled"
            content = result.display_text.strip()
            if result.state in {"completed", "input-required"} and content and history and history[-1][0] == "user":
                history.append(("agent", content))
        self._context = context
        self._task = recovered_task
        self._dispatch_id = dispatch_id
        self._dispatch_phase = restored_dispatch_phase
        self._checkpoint_phase = str(checkpoint_phase or "")
        self._active_state = restored_state
        self._uncertain = restored_uncertain
        self._ready = False
        self._closed = False
        self._detached_for_shutdown = False
        self._generation = (int(generation) if generation is not None else 0) + 1
        self._history = history
        self.last_outcome = result

    async def detach_for_shutdown(self) -> None:
        self._detached_for_shutdown = True

    async def _checkpoint_state(self, phase: str) -> None:
        if phase not in _CHECKPOINT_PHASES:
            raise A2AError(Code.PROTOCOL_ERROR)
        self._checkpoint_phase = phase
        if self._checkpoint is None:
            return
        snapshot = self.export_state()
        snapshot["checkpoint_phase"] = phase
        try:
            await self._checkpoint(snapshot)
        except A2AError:
            raise
        except Exception:
            raise A2AError(Code.OUTCOME_UNKNOWN if self._task is not None else Code.SUBMISSION_UNCERTAIN)

    def _message_config(self) -> dict[str, object]:
        if self._provider == "openclaw":
            return {"blocking": False, "returnImmediately": True}
        return {"blocking": False}

    def _status(self, state: str, reason: str = "") -> None:
        if self._changed is not None:
            self._changed(state, reason)

    async def _rpc(self, method: str, params: dict[str, object]) -> dict[str, object]:
        token = await self._next_token()
        return await self._transport.post_jsonrpc(self.endpoint, {"jsonrpc": "2.0", "id": str(uuid4()), "method": method, "params": params}, token)

    async def initialize(self) -> None:
        async with self._lock:
            if self._closed:
                raise A2AError(Code.CONTEXT_CHANGED)
            if self.needs_recovery:
                if self._active_state == "auth-required":
                    self._status("auth_required", "auth_required")
                    raise A2AError(Code.AUTH_REQUIRED)
                self._status("unknown", "outcome_unknown")
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
                if exc.code in {Code.CREDENTIALS_MISSING, Code.AUTHENTICATION_FAILED}:
                    self._status("auth_required", exc.code.value)
                elif exc.code == Code.OUTCOME_UNKNOWN:
                    self._status("unknown", exc.code.value)
                else:
                    self._status("failed", exc.code.value)
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
            if self._active_state in ACTIVE or self.needs_recovery:
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
            if "taskId" not in message:
                self._task = None
                self._active_state = ""
            message_id = cast(str, message["messageId"])
            self._dispatch_id = message_id
            self.cancellation_status = "none"
            self.last_outcome = None
            self._status("running")
            self._history.append(("user", prompt))
            owned_task_id: str | None = None
            task_id_capture: dict[str, str | None] = {"value": None}
            try:
                self._dispatch_phase = "pre_send"
                await self._checkpoint_state("pre_send")
                if self._streaming:
                    result, owned_task_id = await self._stream_send(message, on_update=on_update, task_id_capture=task_id_capture)
                else:
                    result, owned_task_id = await self._polling_send(message, on_update=on_update, task_id_capture=task_id_capture)
            except asyncio.CancelledError:
                owned_task_id = owned_task_id or task_id_capture["value"]
                if owned_task_id and owned_task_id == self._task and self._active_state in ACTIVE | INTERRUPTED:
                    try:
                        if not self._detached_for_shutdown:
                            async with asyncio.timeout(5):
                                await self._cancel_owned()
                        else:
                            self.cancellation_status = "uncertain"
                            self._unknown(Code.CANCEL_UNCONFIRMED)
                    except (A2AError, TimeoutError):
                        self.cancellation_status = "uncertain"
                        self._unknown(Code.CANCEL_UNCONFIRMED)
                else:
                    self.cancellation_status = "uncertain"
                    self._unknown(Code.CANCEL_UNCONFIRMED)
                raise
            except A2ARPCError as exc:
                # A well-formed RPC error explicitly rejected the request.
                owned_task_id = owned_task_id or task_id_capture["value"]
                if owned_task_id is None:
                    self._dispatch_phase = "settled"
                    self._status("failed", exc.code.value)
                else:
                    self._unknown(Code.OUTCOME_UNKNOWN)
                    raise A2AError(Code.OUTCOME_UNKNOWN) from None
                raise
            except (A2AError, TimeoutError) as exc:
                owned_task_id = owned_task_id or task_id_capture["value"]
                if isinstance(exc, A2AError) and exc.code in {Code.CREDENTIALS_MISSING, Code.AUTHENTICATION_FAILED, Code.UNSUPPORTED} and owned_task_id is None:
                    self._ready = False
                    self._status("auth_required" if exc.code != Code.UNSUPPORTED else "failed", exc.code.value)
                    raise
                code = Code.OUTCOME_UNKNOWN if (owned_task_id == self._task and self._active_state in ACTIVE | INTERRUPTED) else Code.SUBMISSION_UNCERTAIN
                self._unknown(code)
                raise A2AError(code) from None
            self._finish(result)
            final_output = result.display_text.strip()
            if result.state in {"completed", "input-required"} and final_output:
                self._history.append(("agent", final_output))
            await self._checkpoint_state("result")
            if result.state == "input-required":
                raise A2AError(Code.INPUT_REQUIRED, text=final_output)
            if result.state == "auth-required":
                raise A2AError(Code.AUTH_REQUIRED)
            if result.state in {"failed", "rejected", "canceled"}:
                code = {"failed": Code.TASK_FAILED, "rejected": Code.TASK_REJECTED, "canceled": Code.CANCELLED}[result.state]
                raise A2AError(code)
            yield final_output or "Task completed without a text response."

    async def _polling_send(
        self,
        message: dict[str, object],
        on_update: ProgressUpdate | None,
        task_id_capture: dict[str, str | None] | None = None,
    ) -> tuple[A2ARequestResult, str | None]:
        raw = await self._rpc("message/send", {"message": message, "configuration": self._message_config()})
        result = self._accept(raw)
        owned_task_id = result.task_id
        if task_id_capture is not None:
            task_id_capture["value"] = owned_task_id
        self._dispatch_id = str(message["messageId"])
        self._dispatch_phase = "accepted"
        await self._checkpoint_state("send_accepted")
        if on_update is not None:
            await self._emit_update(result, on_update)
        async with asyncio.timeout(self._poll_seconds):
            while result.state in ACTIVE:
                await asyncio.sleep(self._poll_interval)
                raw = await self._rpc("tasks/get", {"id": result.task_id})
                result = self._accept(raw, expected_task=result.task_id)
                await self._checkpoint_state("poll_update")
                if on_update is not None:
                    await self._emit_update(result, on_update)
        return result, owned_task_id

    async def _stream_send(
        self,
        message: dict[str, object],
        on_update: ProgressUpdate | None,
        task_id_capture: dict[str, str | None] | None = None,
    ) -> tuple[A2ARequestResult, str | None]:
        result: A2ARequestResult | None = None
        stream_frame: dict[str, object] = {}
        task_id: str | None = None
        method = "message/stream"
        params: dict[str, object] = {"message": message}
        async with asyncio.timeout(self._poll_seconds):
            for attempt in range(2):
                events = self._rpc_events(method, params)
                try:
                    async for raw in events:
                        result_payload = self._stream_frame(raw, task_id=task_id, carry=stream_frame)
                        result = self._accept(result_payload, expected_task=task_id)
                        task_id = result.task_id
                        if task_id_capture is not None and task_id_capture["value"] is None:
                            task_id_capture["value"] = task_id
                        self._dispatch_phase = "accepted"
                        await self._checkpoint_state("stream_update")
                        if on_update is not None:
                            await self._emit_update(result, on_update)
                        if result.state not in ACTIVE:
                            return result, task_id
                except A2AError as exc:
                    if exc.code != Code.UNREACHABLE or task_id is None or attempt:
                        raise
                finally:
                    await events.aclose()
                if task_id is None:
                    raise A2AError(Code.SUBMISSION_UNCERTAIN)
                if attempt or self._detached_for_shutdown:
                    raise A2AError(Code.OUTCOME_UNKNOWN)
                # Reattach to the known task once. Never repeat message/stream.
                method, params = "tasks/subscribe", {"id": task_id}
        raise A2AError(Code.OUTCOME_UNKNOWN)

    @staticmethod
    def _stream_frame(raw: dict[str, object], *, task_id: str | None,
                      carry: dict[str, object]) -> dict[str, object]:
        """Fold the v1 StreamResponse union, retaining authoritative identity.

        The transport already checked the JSON-RPC envelope. Whole Task
        snapshots replace state; artifact deltas apply only to their own ID.
        """
        variants = [key for key in ("task", "statusUpdate", "artifactUpdate", "message") if key in raw]
        if len(variants) > 1:
            raise A2AError(Code.PROTOCOL_ERROR)
        if variants == ["task"] or not variants:
            snapshot = raw.get("task", raw)
            if not isinstance(snapshot, dict):
                raise A2AError(Code.PROTOCOL_ERROR)
            parse_result(snapshot, expected_task=task_id)
            carry.clear()
            carry.update(snapshot)
        elif variants == ["message"]:
            if task_id is not None:
                raise A2AError(Code.PROTOCOL_ERROR)
            return raw
        else:
            variant = variants[0]
            event = raw[variant]
            if not isinstance(event, dict):
                raise A2AError(Code.PROTOCOL_ERROR)
            identity = _identifier(event.get("taskId"), required=True)
            context = _identifier(event.get("contextId"), required=True)
            if (task_id is not None and identity != task_id) or (carry.get("contextId") is not None and context != carry["contextId"]):
                raise A2AError(Code.PROTOCOL_ERROR)
            carry["id"], carry["contextId"] = identity, context
            if variant == "statusUpdate":
                status = event.get("status")
                if not isinstance(status, dict):
                    raise A2AError(Code.PROTOCOL_ERROR)
                _normalize_state(status.get("state"))
                carry["status"] = status
            else:
                artifact = event.get("artifact")
                if not isinstance(artifact, dict):
                    raise A2AError(Code.PROTOCOL_ERROR)
                artifact_id = _identifier(artifact.get("artifactId"), required=True)
                for flag in ("append", "lastChunk"):
                    if flag in event and type(event[flag]) is not bool:
                        raise A2AError(Code.PROTOCOL_ERROR)
                text = _text(artifact.get("parts"))
                old = carry.get("artifacts", [])
                if not isinstance(old, list) or not all(isinstance(item, dict) for item in old):
                    raise A2AError(Code.PROTOCOL_ERROR)
                artifacts = [dict(item) for item in old]
                index = next((i for i, item in enumerate(artifacts) if item.get("artifactId") == artifact_id), None)
                if index is not None and event.get("append", False):
                    text = _text(artifacts[index].get("parts", [])) + text
                updated = {"artifactId": artifact_id, "parts": [{"text": text}]}
                if index is None:
                    artifacts.append(updated)
                else:
                    artifacts[index] = updated
                carry["artifacts"] = artifacts
        if len(json.dumps(carry, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_BYTES:
            raise A2AError(Code.PROTOCOL_ERROR)
        return dict(carry)

    async def _rpc_events(self, method: str, params: dict[str, object]) -> AsyncGenerator[dict[str, object], None]:
        token = await self._next_token()
        events = self._transport.post_jsonrpc_events(
            self.endpoint, {"jsonrpc": "2.0", "id": str(uuid4()), "method": method, "params": params}, token,
        )
        try:
            async for payload in events:
                yield payload
        finally:
            close = getattr(events, "aclose", None)
            if close is not None:
                await close()

    async def _next_token(self) -> str:
        try:
            token = await self._token_loader()
        except Exception:
            raise A2AError(Code.CREDENTIALS_MISSING) from None
        if not isinstance(token, str) or not token or len(token) > 8192 or any(c.isspace() or ord(c) < 32 for c in token):
            raise A2AError(Code.CREDENTIALS_MISSING)
        return token

    async def _emit_update(self, result: A2ARequestResult, on_update: ProgressUpdate) -> None:
        key = (result.task_id, result.context_id, result.state, result.text, result.status_text)
        if self._last_update == key:
            return
        self._last_update = key
        await self._checkpoint_state("emit_update")
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
        self._dispatch_phase = "settled"
        self._status(state, reason)

    def _unknown(self, reason: Code) -> None:
        self._uncertain = True
        self._generation += 1
        self._status("unknown", "outcome_unknown" if reason == Code.SUBMISSION_UNCERTAIN else reason.value)
        self._dispatch_phase = "uncertain"

    async def _cancel_owned(self) -> None:
        if self._task is None:
            raise A2AError(Code.OUTCOME_UNKNOWN)
        self._dispatch_phase = "cancel_pending"
        await self._checkpoint_state("cancel_pending")
        raw = await self._rpc("tasks/cancel", {"id": self._task})
        result = self._accept(raw, expected_task=self._task)
        if result.state != "canceled":
            raise A2AError(Code.CANCEL_UNCONFIRMED)
        self.cancellation_status = "confirmed"
        self._uncertain = False
        self._finish(result)
        await self._checkpoint_state("cancel_result")

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
            previous = self.last_outcome
            raw = await self._rpc("tasks/get", {"id": self._task})
            result = self._accept(raw, expected_task=self._task)
            if result.state in ACTIVE:
                self._uncertain = True
                self._status("unknown", "outcome_unknown")
                await self._checkpoint_state("recover")
                return "The agent is still working. Check again or cancel this task."
            final_output = result.display_text.strip()
            self._ready = True  # The authenticated owned-task query succeeded.
            self._finish(result)
            if result.state in {"completed", "input-required"} and final_output and result != previous:
                self._history.append(("agent", final_output))
            await self._checkpoint_state("recover")
            if result.state == "auth-required":
                return provider_permissions(self._provider).authentication_guidance
            return final_output or f"Agent task: {result.state}."

    async def reset_conversation(self) -> None:
        async with self._lock:
            if self._closed or self._active_state in ACTIVE | INTERRUPTED:
                raise A2AError(Code.OUTCOME_UNKNOWN)
            self._context = None
            self._task = None
            self._dispatch_id = None
            self._dispatch_phase = "reset"
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
        self._dispatch_phase = "closed"
        self._generation += 1
        self._status("closed")
