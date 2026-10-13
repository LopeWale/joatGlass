"""Bounded shell-child commands. This module never speaks the Hub protocol."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import asdict, dataclass
import json
import logging
import math
import os
import sys
from typing import Any, BinaryIO
from uuid import UUID

from ..core.pairing import ShellPairingStore, is_pairing_token

log = logging.getLogger(__name__)

ACTIONS = frozenset({"news.refresh", "service.retry"})
MAX_FRAME = 16_384
MAX_REQUESTS = 4096
REASONS = frozenset({"completed", "partial", "unavailable", "not_configured", "denied",
                     "busy", "invalid_request", "duplicate", "timeout", "cancelled",
                     "superseded", "owner_failed", "identity_mismatch"})


@dataclass(frozen=True)
class Capability:
    configured: bool = False
    available: bool = False
    active: bool = False
    denied: bool = False
    disconnected: bool = True
    pending: bool = False


def service_retry_capability(*, configured: bool, owner_available: bool,
                             state: str, busy: bool) -> Capability:
    """Saved configuration and a usable runtime owner are separate facts."""
    active = owner_available and state in {"ready", "running"}
    return Capability(
        configured=configured,
        available=configured and owner_available and not busy and state != "running",
        active=active, denied=state in {"approval_required", "blocked"},
        disconnected=not active,
    )


@dataclass(frozen=True)
class CommandResult:
    outcome: str
    reason: str

    def __post_init__(self) -> None:
        if self.outcome not in {"succeeded", "refused", "failed"} or self.reason not in REASONS:
            raise ValueError("invalid command result")


Owner = Callable[[], Coroutine[Any, Any, CommandResult]]
Projection = Callable[[], Mapping[str, Capability]]
Emit = Callable[[dict[str, Any]], None]


class LocalCommandDispatcher:
    """Identity, replay and cancellation fence around two existing owners."""

    def __init__(self, *, epoch: str, parent_pid: int, engine_pid: int,
                 owners: Mapping[str, Owner], projection: Projection, emit: Emit,
                 current_parent: Callable[[], int] = os.getppid) -> None:
        UUID(epoch)
        if set(owners) != ACTIONS:
            raise ValueError("exactly two command owners required")
        self.identity = dict(v=1, epoch=epoch, parent_pid=parent_pid, engine_pid=engine_pid)
        self._owners = owners
        self._projection = projection
        self._emit = emit
        self._current_parent = current_parent
        self._seen: set[str] = set()
        self._requests: dict[str, tuple[str, asyncio.Task[None]]] = {}
        self._operations: dict[str, asyncio.Task[CommandResult]] = {}
        self._closed = False

    def capabilities(self) -> dict[str, dict[str, bool]]:
        source = self._projection()
        result: dict[str, dict[str, bool]] = {}
        for action in sorted(ACTIONS):
            item = source.get(action, Capability())
            # Serialize only our fixed boolean schema, never arbitrary owner data.
            values = {key: value is True for key, value in asdict(item).items()}
            values["pending"] = action in self._requests
            result[action] = values
        return result

    def publish(self, kind: str = "capabilities", **extra: Any) -> None:
        if not self._closed:
            self._emit({**self.identity, "kind": kind, "capabilities": self.capabilities(), **extra})

    def is_from_parent(self, message: Any) -> bool:
        """The exact launch identity, from the parent that still owns us."""
        if self._closed or self._current_parent() != self.identity["parent_pid"]:
            return False
        return isinstance(message, dict) and all(
            type(message.get(k)) is type(v) and message.get(k) == v
            for k, v in self.identity.items()
        )

    def _ack(self, request_id: str, action: str, outcome: str, reason: str) -> None:
        if not self._closed:
            self._emit({**self.identity, "kind": "ack", "request_id": request_id,
                        "action": action, "outcome": outcome, "reason": reason,
                        "capabilities": self.capabilities()})

    def receive(self, message: Any) -> None:
        if self._closed:
            return
        if self._current_parent() != self.identity["parent_pid"]:
            self.close()
            return
        if not isinstance(message, dict) or any(
            type(message.get(k)) is not type(v) or message.get(k) != v
            for k, v in self.identity.items()
        ):
            return  # Untrusted identities get no acknowledgement or state.
        request_id = message.get("request_id")
        if not isinstance(request_id, str) or len(request_id) != 36:
            return
        try:
            UUID(request_id)
        except ValueError:
            return
        if message.get("kind") == "cancel":
            for pending_action, (pending_id, _) in tuple(self._requests.items()):
                if pending_id == request_id:
                    self._retire(pending_action, "cancelled", "cancelled")
            return
        action = message.get("action")
        if message.get("kind") != "request" or not isinstance(action, str) or action not in ACTIONS:
            return
        if request_id in self._seen:
            return
        timeout = message.get("timeout_ms")
        if type(timeout) is not int or not 100 <= timeout <= 30_000:
            self._ack(request_id, action, "refused", "invalid_request")
            return
        if len(self._seen) >= MAX_REQUESTS:
            self._ack(request_id, action, "refused", "busy")
            return
        self._seen.add(request_id)
        if action in self._requests:
            self._retire(action, "superseded", "superseded")
        capability = self._projection().get(action, Capability())
        if not capability.configured:
            self._ack(request_id, action, "refused", "not_configured")
            return
        if not capability.available:
            self._ack(request_id, action, "refused", "denied" if capability.denied else "busy")
            return
        task = asyncio.create_task(self._run(request_id, action, timeout / 1000))
        self._requests[action] = (request_id, task)
        self._ack(request_id, action, "pending", "completed")

    def _retire(self, action: str, outcome: str, reason: str) -> None:
        request_id, task = self._requests.pop(action)
        task.cancel()
        operation = self._operations.get(action)
        if operation is not None and not operation.done():
            operation.cancel()
        self._ack(request_id, action, outcome, reason)

    async def _run(self, request_id: str, action: str, timeout: float) -> None:
        result = CommandResult("failed", "owner_failed")
        outcome, reason = result.outcome, result.reason
        try:
            async with asyncio.timeout(timeout):
                # A cancelled owner must drain before its replacement enters.
                prior = self._operations.get(action)
                if prior is not None and not prior.done():
                    await asyncio.wait({prior})
                if self._closed:
                    return
                operation = asyncio.create_task(self._owners[action]())
                self._operations[action] = operation
                # Retrieve exceptions even when the caller has already retired.
                operation.add_done_callback(lambda task: None if task.cancelled() else task.exception())
                result = await asyncio.shield(operation)
                outcome, reason = result.outcome, result.reason
        except TimeoutError:
            timed_operation = self._operations.get(action)
            if timed_operation is not None and not timed_operation.done():
                timed_operation.cancel()
            outcome, reason = "timed_out", "timeout"
        except asyncio.CancelledError:
            return
        except Exception:
            pass  # Fixed failure code; no provider/error content on this wire.
        current = self._requests.get(action)
        if current is not None and current[0] == request_id:
            self._requests.pop(action)
            self._ack(request_id, action, outcome, reason)

    def close(self) -> None:
        self._closed = True
        for _, task in self._requests.values():
            task.cancel()
        for operation in self._operations.values():
            operation.cancel()
        self._requests.clear()


class ChildCommandPipe:
    """Anonymous pipes inherited only from the parent shell; no socket or token."""

    def __init__(self, epoch: str, parent_pid: int, output: BinaryIO, input_pipe: BinaryIO) -> None:
        self.epoch = epoch
        self.parent_pid = parent_pid
        self.output = output
        self.input = input_pipe

    @classmethod
    def from_environment(cls) -> ChildCommandPipe | None:
        epoch = os.environ.pop("JOAT_COMMAND_EPOCH", "")
        parent = os.environ.pop("JOAT_COMMAND_PARENT_PID", "")
        if not epoch and not parent:
            return None
        UUID(epoch)
        if not parent.isdecimal() or int(parent) != os.getppid():
            raise ValueError("command parent identity mismatch")
        # Reserve fd 1 before any owner starts. Libraries/subprocesses inherit
        # stderr as stdout; the protocol descriptor is non-inheritable.
        output = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
        os.set_blocking(output.fileno(), False)
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        input_pipe = os.fdopen(os.dup(sys.stdin.fileno()), "rb", buffering=0)
        # CLI children must not inherit the command reader and steal requests.
        with open(os.devnull, "rb") as null_input:
            os.dup2(null_input.fileno(), sys.stdin.fileno())
        return cls(epoch, int(parent), output, input_pipe)

    async def run(self, owners: Mapping[str, Owner], projection: Projection,
                  pairing: ShellPairingStore | None = None) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=MAX_FRAME)
        transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), self.input)
        def emit(payload: dict[str, Any]) -> None:
            data = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
            if self.output.write(data) != len(data):
                raise BlockingIOError("command pipe backpressure")
        dispatcher = LocalCommandDispatcher(
            epoch=self.epoch, parent_pid=self.parent_pid, engine_pid=os.getpid(),
            owners=owners, projection=projection, emit=emit,
        )
        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(2)
                if os.getppid() != self.parent_pid:
                    dispatcher.close()
                    return
                dispatcher.publish()
        heartbeat_task = asyncio.create_task(heartbeat())
        # A broken/backpressured response pipe retires the whole session.
        def heartbeat_finished(task: asyncio.Task[None]) -> None:
            if not task.cancelled() and task.exception() is not None:
                dispatcher.close()
                transport.close()
        heartbeat_task.add_done_callback(heartbeat_finished)
        if pairing is not None:
            def confirmed(token: str) -> None:
                emit({**dispatcher.identity, "kind": "pairing.confirmed", "token": token})
            pairing.set_on_confirmed(confirmed)
        try:
            # "pairing": the app may move the key into its keychain and send
            # it here. An engine without it keeps using pairing.json.
            dispatcher.publish("hello", **({"pairing": 1} if pairing is not None else {}))
            while data := await reader.readline():
                if len(data) > MAX_FRAME:
                    break
                try:
                    message = json.loads(data)
                except (ValueError, UnicodeDecodeError):
                    break
                if isinstance(message, dict) and message.get("kind") == "pairing.state":
                    if pairing is not None and dispatcher.is_from_parent(message):
                        _apply_pairing_state(pairing, message)
                    continue
                dispatcher.receive(message)
        except (OSError, ValueError):
            pass
        finally:
            if pairing is not None:
                pairing.set_on_confirmed(None)
            dispatcher.close()
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            transport.close()
            self.output.close()


def _apply_pairing_state(pairing: ShellPairingStore, message: Mapping[str, Any]) -> None:
    """JOAT.app's keychain state: its key, or none and maybe a window.

    Anything malformed is ignored, leaving the previous state: a bad frame
    can never open a window or replace the key.
    """
    token = message.get("token")
    window = message.get("window_expires_at")
    if token is not None and not is_pairing_token(token):
        log.warning("ignored a pairing state with an invalid key")
        return
    if window is not None and (
        type(window) not in (int, float) or not math.isfinite(window)
    ):
        log.warning("ignored a pairing state with an invalid window")
        return
    pairing.apply(token, None if window is None else float(window))
    log.info("pairing state from JOAT.app: %s", (
        "paired" if token is not None
        else "pairing window open" if pairing.window_open
        else "not paired"
    ))
