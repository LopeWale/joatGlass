"""The work lane: approvals and roster, WITHOUT loosening the Brainstorm lane.

Every capability here was verified against docs.openclaw.ai/gateway/protocol
(sessions.list with involvingMe; session.approval events opted in via
sessions.messages.subscribe includeApprovals; answered with approval.resolve
{id, kind, decision}, first answer wins) — not assumed. The tests are pure
policy checks, so they run even in the socket-blocked sandbox.

The design rule under test: the Brainstorm lane's approval/tool tripwire is
load-bearing safety. Approvals arrive via a NEW lane with its own flags and
scopes — a policy edit that let the Brainstorm lane resolve an approval
would be a boundary breach, and these tests are the alarm.
"""

from __future__ import annotations

import pytest

from joat.agents.openclaw_gateway import (
    AgentTerminalPolicy,
    BRAINSTORM_TERMINAL_POLICY,
    OpenClawGatewayError,
    WORK_TERMINAL_POLICY,
    _enforce_agent_terminal_request_policy as enforce,
)


def work_key() -> str:
    return WORK_TERMINAL_POLICY.new_session_key("ab12")


class TestLaneConstruction:
    def test_the_work_lane_is_a_known_complete_lane(self) -> None:
        assert WORK_TERMINAL_POLICY.allows_approvals is True
        assert WORK_TERMINAL_POLICY.roster is True

    def test_no_other_lane_may_carry_the_approval_flag(self) -> None:
        # The whitelist includes the flags in the identity, so a caller
        # cannot mint a Brainstorm-shaped lane with approvals bolted on.
        with pytest.raises(ValueError):
            AgentTerminalPolicy(
                agent_id="hermes",
                session_label="JOAT Agent Terminal",
                session_key_namespace="joat-",
                client_display_name="JOAT Brainstorm",
                allows_approvals=True,
            )


class TestApprovalResolve:
    def test_work_lane_may_answer_an_approval(self) -> None:
        enforce(
            "approval.resolve",
            {"id": "appr-42", "kind": "exec", "decision": "deny"},
            policy=WORK_TERMINAL_POLICY,
        )

    def test_brainstorm_lane_may_never_answer_one(self) -> None:
        with pytest.raises(OpenClawGatewayError):
            enforce(
                "approval.resolve",
                {"id": "appr-42", "kind": "exec", "decision": "deny"},
                policy=BRAINSTORM_TERMINAL_POLICY,
            )

    @pytest.mark.parametrize("params", [
        {"id": "appr-42", "kind": "exec", "decision": "yes"},        # not a decision
        {"id": "appr-42", "kind": "exec", "decision": "approve", "note": "x"},
        {"id": "", "kind": "exec", "decision": "approve"},
        {"id": "appr-42", "kind": "", "decision": "approve"},
        {"id": "appr-42", "decision": "approve"},                    # kind missing
    ])
    def test_malformed_resolutions_are_rejected_even_on_the_work_lane(
        self, params: dict[str, object]
    ) -> None:
        with pytest.raises(OpenClawGatewayError):
            enforce("approval.resolve", params, policy=WORK_TERMINAL_POLICY)


class TestApprovalSubscription:
    def test_work_lane_may_opt_into_approval_events(self) -> None:
        enforce(
            "sessions.messages.subscribe",
            {"key": work_key(), "agentId": "hermes", "includeApprovals": True},
            policy=WORK_TERMINAL_POLICY,
        )

    def test_brainstorm_lane_asking_for_approvals_is_a_violation(self) -> None:
        key = BRAINSTORM_TERMINAL_POLICY.new_session_key("ab12")
        with pytest.raises(OpenClawGatewayError):
            enforce(
                "sessions.messages.subscribe",
                {"key": key, "agentId": "hermes", "includeApprovals": True},
                policy=BRAINSTORM_TERMINAL_POLICY,
            )

    def test_plain_subscription_still_works_everywhere(self) -> None:
        key = BRAINSTORM_TERMINAL_POLICY.new_session_key("ab12")
        enforce("sessions.messages.subscribe",
                {"key": key, "agentId": "hermes"},
                policy=BRAINSTORM_TERMINAL_POLICY)

    def test_include_approvals_false_is_not_a_loophole(self) -> None:
        # Only the literal documented opt-in shape passes; a False flag is
        # noise that should have been omitted.
        with pytest.raises(OpenClawGatewayError):
            enforce(
                "sessions.messages.subscribe",
                {"key": work_key(), "agentId": "hermes", "includeApprovals": False},
                policy=WORK_TERMINAL_POLICY,
            )


class TestRosterListing:
    def test_work_lane_enumerates_only_the_wearers_own_sessions(self) -> None:
        enforce(
            "sessions.list",
            {"agentId": "hermes", "limit": 20, "involvingMe": True},
            policy=WORK_TERMINAL_POLICY,
        )

    def test_brainstorm_lane_stays_pinned_to_its_labelled_session(self) -> None:
        with pytest.raises(OpenClawGatewayError):
            enforce(
                "sessions.list",
                {"agentId": "hermes", "limit": 20, "involvingMe": True},
                policy=BRAINSTORM_TERMINAL_POLICY,
            )

    @pytest.mark.parametrize("params", [
        {"agentId": "hermes", "limit": 999, "involvingMe": True},   # unbounded
        {"agentId": "hermes", "limit": True, "involvingMe": True},  # bool trap
        {"agentId": "hermes", "limit": 20, "involvingMe": False},   # everyone's?
        {"agentId": "hermes", "limit": 20},                          # filter dropped
        {"agentId": "hermes", "limit": 20, "involvingMe": True, "ownerId": "x"},
    ])
    def test_roster_listing_never_becomes_an_arbitrary_filter_surface(
        self, params: dict[str, object]
    ) -> None:
        with pytest.raises(OpenClawGatewayError):
            enforce("sessions.list", params, policy=WORK_TERMINAL_POLICY)

    def test_the_pinned_two_session_list_still_works_on_old_lanes(self) -> None:
        enforce(
            "sessions.list",
            {"label": "JOAT Agent Terminal", "agentId": "hermes", "limit": 2},
            policy=BRAINSTORM_TERMINAL_POLICY,
        )


class TestScopeCoherence:
    """operator.approvals only rides on a full read+write session."""

    def test_the_work_lane_triple_validates(self) -> None:
        from joat.agents.openclaw_gateway import _validated_operator_scopes

        triple = ["operator.read", "operator.write", "operator.approvals"]
        assert _validated_operator_scopes(triple) == triple

    def test_bare_approvals_is_still_a_config_typo(self) -> None:
        # This rejection predates the work lane and survives it — widening
        # ALLOWED_OPERATOR_SCOPES must not make incoherent authority valid.
        from joat.agents.openclaw_gateway import _validated_operator_scopes

        with pytest.raises(ValueError):
            _validated_operator_scopes(["operator.approvals"])
        with pytest.raises(ValueError):
            _validated_operator_scopes(["operator.read", "operator.approvals"])


class FakeConnection:
    """Just enough of AgentTerminalConnection for a direct terminal call."""

    advertised_methods = frozenset()
    advertised_events = frozenset()
    granted_scopes: tuple[str, ...] = ()

    def __init__(self, payload: object) -> None:
        self.requests: list[tuple[str, dict[str, object]]] = []
        self._payload = payload

    async def request(self, method: str, params: dict[str, object]) -> object:
        self.requests.append((method, params))
        return self._payload

    async def next_event(self) -> dict[str, object]:  # pragma: no cover
        raise AssertionError("unused")

    async def close(self) -> None:  # pragma: no cover
        ...


class TestListSessions:
    async def test_issues_the_roster_shape_and_sanitizes_rows(self) -> None:
        from joat.agents.openclaw_terminal import OpenClawAgentTerminal

        connection = FakeConnection({"sessions": [
            {"key": "agent:hermes:joat-work-aa", "label": "deploys", "sessionId": "s1"},
            {"key": 42, "label": "corrupt row"},               # skipped, not guessed
            {"key": "agent:hermes:joat-work-bb"},              # missing fields tolerated
            "not even a record",
        ]})
        terminal = OpenClawAgentTerminal(
            connection=connection, agent_id="hermes",
            session_key=WORK_TERMINAL_POLICY.new_session_key("ab12"),
            session_id="sess", policy=WORK_TERMINAL_POLICY,
        )
        rows = await terminal.list_sessions(limit=20)
        assert connection.requests == [
            ("sessions.list", {"agentId": "hermes", "limit": 20, "involvingMe": True}),
        ]
        assert rows == [
            {"key": "agent:hermes:joat-work-aa", "label": "deploys", "sessionId": "s1"},
            {"key": "agent:hermes:joat-work-bb", "label": "", "sessionId": ""},
        ]

    async def test_a_non_list_payload_is_an_error_not_an_empty_roster(self) -> None:
        from joat.agents.openclaw_terminal import OpenClawAgentTerminal

        terminal = OpenClawAgentTerminal(
            connection=FakeConnection({"sessions": "nope"}), agent_id="hermes",
            session_key=WORK_TERMINAL_POLICY.new_session_key("ab12"),
            session_id="sess", policy=WORK_TERMINAL_POLICY,
        )
        with pytest.raises(OpenClawGatewayError):
            await terminal.list_sessions()


class TestResumableTargets:
    """A roster lane may address a key ONLY after the gateway listed it.

    Provenance is the whole design: the allowed target set is what the
    gateway itself named for this wearer on this connection — never a key
    the Mac invented, and never anything on the Brainstorm lane.
    """

    LISTED = frozenset({"agent:hermes:someone-elses-namespace-1"})

    def _send(self, key: str) -> dict[str, object]:
        return {"key": key, "agentId": "hermes", "message": "continue",
                "idempotencyKey": "a" * 32}

    def test_a_listed_key_becomes_sendable_on_the_roster_lane(self) -> None:
        enforce("sessions.send",
                self._send("agent:hermes:someone-elses-namespace-1"),
                policy=WORK_TERMINAL_POLICY, listed_keys=self.LISTED)

    def test_an_unlisted_key_stays_blocked(self) -> None:
        with pytest.raises(OpenClawGatewayError):
            enforce("sessions.send",
                    self._send("agent:hermes:invented-by-the-mac"),
                    policy=WORK_TERMINAL_POLICY, listed_keys=self.LISTED)

    def test_the_brainstorm_lane_gains_nothing_from_listings(self) -> None:
        with pytest.raises(OpenClawGatewayError):
            enforce("sessions.send",
                    self._send("agent:hermes:someone-elses-namespace-1"),
                    policy=BRAINSTORM_TERMINAL_POLICY, listed_keys=self.LISTED)

    def test_abort_follows_the_same_provenance(self) -> None:
        params = {"key": "agent:hermes:someone-elses-namespace-1",
                  "runId": "run-1", "agentId": "hermes"}
        enforce("sessions.abort", params,
                policy=WORK_TERMINAL_POLICY, listed_keys=self.LISTED)
        with pytest.raises(OpenClawGatewayError):
            enforce("sessions.abort", params, policy=WORK_TERMINAL_POLICY)

    def test_owned_keys_still_work_without_any_listing(self) -> None:
        enforce("sessions.send", self._send(work_key()),
                policy=WORK_TERMINAL_POLICY)


class TestHarvest:
    def test_only_string_keys_survive(self) -> None:
        from joat.agents.openclaw_gateway import _harvest_listed_keys

        assert _harvest_listed_keys({"sessions": [
            {"key": "agent:hermes:a"},
            {"key": 42}, {"nokey": True}, "junk",
            {"key": "x" * 300},              # oversized: refused
        ]}) == {"agent:hermes:a"}
        assert _harvest_listed_keys("garbage") == set()
