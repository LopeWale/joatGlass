from __future__ import annotations

import asyncio
import signal

from joat.services.shutdown import (
    install_shutdown_handlers,
    remove_shutdown_handlers,
    wait_for_supervisor_exit,
)


class FakeSignalLoop:
    def __init__(self) -> None:
        self.handlers: dict[signal.Signals, object] = {}
        self.removed: list[signal.Signals] = []

    def add_signal_handler(
        self,
        process_signal: signal.Signals,
        callback: object,
        *args: object,
    ) -> None:
        self.handlers[process_signal] = callback

    def remove_signal_handler(self, process_signal: signal.Signals) -> bool:
        self.removed.append(process_signal)
        return self.handlers.pop(process_signal, None) is not None


def test_sigterm_sets_shutdown_and_handler_is_removed() -> None:
    async def exercise() -> None:
        shutdown = asyncio.Event()
        loop = FakeSignalLoop()
        installed = install_shutdown_handlers(
            shutdown,
            loop=loop,  # type: ignore[arg-type]
        )

        callback = loop.handlers[signal.SIGTERM]
        assert callable(callback)
        callback()

        assert shutdown.is_set()
        remove_shutdown_handlers(
            installed,
            loop=loop,  # type: ignore[arg-type]
        )
        assert loop.removed == [signal.SIGTERM]

    asyncio.run(exercise())


async def test_supervisor_reparenting_is_detected() -> None:
    parents = iter((42, 42, 1))
    sleeps: list[float] = []

    async def no_wait(delay: float) -> None:
        sleeps.append(delay)

    assert await wait_for_supervisor_exit(
        getppid=lambda: next(parents),
        sleep=no_wait,
        poll_interval_s=0.25,
    )
    assert sleeps == [0.25]


async def test_detached_engine_has_no_supervisor_to_watch() -> None:
    async def unexpected_sleep(_: float) -> None:
        raise AssertionError("detached engine must not poll")

    assert not await wait_for_supervisor_exit(
        getppid=lambda: 1,
        sleep=unexpected_sleep,
    )


async def test_reparenting_before_watchdog_starts_is_still_detected() -> None:
    assert await wait_for_supervisor_exit(
        initial_parent_pid=42,
        getppid=lambda: 1,
    )
