"""Cooperative shutdown for the macOS-supervised engine.

The JOAT shell owns the Python engine, and the engine owns long-lived child
processes such as ``whisperkit-cli serve``.  A plain SIGTERM would otherwise
skip asyncio cleanup and leave those children reparented to launchd.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Awaitable, Callable, Iterable


def install_shutdown_handlers(
    shutdown: asyncio.Event,
    *,
    loop: asyncio.AbstractEventLoop | None = None,
    signals: Iterable[signal.Signals] = (signal.SIGTERM,),
) -> tuple[signal.Signals, ...]:
    """Arrange for process termination to enter the normal cleanup path."""
    event_loop = loop or asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for process_signal in signals:
        try:
            event_loop.add_signal_handler(process_signal, shutdown.set)
        except NotImplementedError:  # pragma: no cover - macOS supports this
            continue
        installed.append(process_signal)
    return tuple(installed)


def remove_shutdown_handlers(
    installed: Iterable[signal.Signals],
    *,
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """Remove only handlers installed by :func:`install_shutdown_handlers`."""
    event_loop = loop or asyncio.get_running_loop()
    for process_signal in installed:
        event_loop.remove_signal_handler(process_signal)


async def wait_for_supervisor_exit(
    *,
    initial_parent_pid: int | None = None,
    getppid: Callable[[], int] = os.getppid,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    poll_interval_s: float = 2.0,
) -> bool:
    """Return true when a supervised engine is reparented to PID 1.

    A process that started with PID 1 as its parent was intentionally launched
    from a terminal or launchd, so there is no shell supervisor to watch.
    """
    parent_pid = getppid() if initial_parent_pid is None else initial_parent_pid
    if parent_pid == 1:
        return False
    while getppid() != 1:
        await sleep(poll_interval_s)
    return True
