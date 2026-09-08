"""The topic-capture arm expires. An arm that does not is ambient capture.

A tap promises "the next phrase is the topic", and that path deliberately
bypasses the wake gate — the gesture already gave consent. Consent to the
NEXT thing the wearer says does not extend to whatever they happen to say
an hour after a forgotten tap, with two paid collaborators on the other
end. AgentTerminalCommands.ARM_WINDOW_S bounds the sibling promise for
the same reason and with the same number.
"""

from __future__ import annotations

from joat.services.session import SessionManager


class FakeHud:
    async def show(self, *a: object, **k: object) -> None: ...
    async def show_brainstorm(self, *a: object, **k: object) -> None: ...


class Clock:
    """The manager's injected monotonic source.

    SessionManager takes its clock as a constructor argument and binds it
    once, so patching joat.services.session.time.monotonic afterwards could
    never move this manager's idea of "now" — the arm looked eternal to a
    test that was really measuring nothing. Drive the real seam instead.
    """

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def manager(clock: Clock | None = None) -> SessionManager:
    return SessionManager(  # type: ignore[arg-type]
        FakeHud(), lambda topic: None, monotonic=clock or Clock(),
    )


class TestTheArmExpires:
    def test_fresh_manager_is_not_armed(self) -> None:
        assert manager().topic_capture_armed is False

    def test_arming_holds_inside_the_window_and_lapses_after(self) -> None:
        clock = Clock()
        m = manager(clock)
        armed_at = clock.now
        m._arm_topic_capture()
        assert m.topic_capture_armed is True
        for delta, expected in ((m.TOPIC_CAPTURE_WINDOW_S - 1, True),
                                (m.TOPIC_CAPTURE_WINDOW_S + 1, False)):
            clock.now = armed_at + delta
            assert m.topic_capture_armed is expected, delta

    def test_disarm_is_immediate(self) -> None:
        m = manager()
        m._arm_topic_capture()
        m._disarm_topic_capture()
        assert m.topic_capture_armed is False

    def test_the_window_matches_the_agent_terminal_promise(self) -> None:
        from joat.services.agent_terminal import AgentTerminalCommands

        assert m if (m := SessionManager.TOPIC_CAPTURE_WINDOW_S) else True
        assert SessionManager.TOPIC_CAPTURE_WINDOW_S \
            == AgentTerminalCommands.ARM_WINDOW_S
