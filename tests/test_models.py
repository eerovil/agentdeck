"""Unit tests for provider-neutral model policy."""

from __future__ import annotations

import pytest

from agentdeck.models import (
    CONTROL_CAPABILITIES,
    Capability,
    InjectResult,
    runtime_control_capabilities,
    runtime_turn_state,
)


def test_unavailable_runtime_grants_no_control_capabilities():
    # An owned session whose runtime is unreachable is read-only: neither an
    # active turn nor a pending interaction can grant control while unavailable.
    assert (
        runtime_control_capabilities(
            available=False, active_turn=True, actionable_interaction=True
        )
        == frozenset()
    )


def test_available_idle_grants_inject_only():
    assert runtime_control_capabilities(
        available=True, active_turn=False, actionable_interaction=False
    ) == frozenset({Capability.INJECT})


def test_active_turn_adds_steer_and_interrupt():
    assert runtime_control_capabilities(
        available=True, active_turn=True, actionable_interaction=False
    ) == frozenset({Capability.INJECT, Capability.STEER, Capability.INTERRUPT})


def test_actionable_interaction_adds_interact():
    assert runtime_control_capabilities(
        available=True, active_turn=False, actionable_interaction=True
    ) == frozenset({Capability.INJECT, Capability.INTERACT})


def test_active_turn_and_interaction_together():
    assert runtime_control_capabilities(
        available=True, active_turn=True, actionable_interaction=True
    ) == CONTROL_CAPABILITIES


@pytest.mark.parametrize("active_turn", [False, True])
@pytest.mark.parametrize("actionable_interaction", [False, True])
def test_result_is_always_within_the_control_set(active_turn, actionable_interaction):
    # The policy never grants a read-only capability (TRANSCRIPT/DEEPLINK); those
    # are derived separately and merged by each provider.
    result = runtime_control_capabilities(
        available=True,
        active_turn=active_turn,
        actionable_interaction=actionable_interaction,
    )
    assert result <= CONTROL_CAPABILITIES
    assert Capability.TRANSCRIPT not in result
    assert Capability.DEEPLINK not in result


def test_actionable_interaction_outranks_stall_and_suppresses_thinking():
    # An answerable interaction overrides stall evidence and forces thinking off,
    # regardless of turn activity.
    assert runtime_turn_state(
        active_turn=True, actionable_interaction=True, stalled_evidence=True
    ) == (False, False)


def test_stall_evidence_shows_without_interaction():
    assert runtime_turn_state(
        active_turn=True, actionable_interaction=False, stalled_evidence=True
    ) == (True, False)


def test_thinking_requires_active_turn_no_interaction_and_not_stalled():
    assert runtime_turn_state(
        active_turn=True, actionable_interaction=False, stalled_evidence=False
    ) == (False, True)


def test_idle_turn_is_neither_stalled_nor_thinking():
    assert runtime_turn_state(
        active_turn=False, actionable_interaction=False, stalled_evidence=False
    ) == (False, False)


def test_control_set_excludes_read_only_capabilities():
    assert Capability.TRANSCRIPT not in CONTROL_CAPABILITIES
    assert Capability.DEEPLINK not in CONTROL_CAPABILITIES
    assert CONTROL_CAPABILITIES == frozenset(
        {
            Capability.INJECT,
            Capability.STEER,
            Capability.INTERRUPT,
            Capability.INTERACT,
        }
    )


def test_pending_interaction_from_dict_round_trips_asdict():
    from dataclasses import asdict

    from agentdeck.models import (
        InteractionOption,
        InteractionQuestion,
        PendingInteraction,
    )

    x = PendingInteraction(
        id="int-1",
        kind="permission",
        thread_id="thread-9",
        turn_id="turn-2",
        title="Allow Bash?",
        message="run the tests",
        questions=(
            InteractionQuestion(
                id="q0",
                header="Pick",
                prompt="Which?",
                options=(
                    InteractionOption(label="A", description="first", value="a"),
                    InteractionOption(label="B"),
                ),
                allow_other=True,
                secret=True,
                multiselect=True,  # the field the Codex deserializer used to drop
            ),
        ),
        command="pytest -q",
        cwd="/repo",
        url="https://example/pr/1",
        decisions=("accept", "decline"),
    )
    # from_dict is the exact inverse of asdict — pins the round-trip and guards
    # future field additions from silently diverging deserializers.
    assert PendingInteraction.from_dict(asdict(x)) == x


def test_pending_interaction_from_dict_rejects_a_non_dict_or_missing_id():
    from agentdeck.models import PendingInteraction

    assert PendingInteraction.from_dict(None) is None
    assert PendingInteraction.from_dict({"kind": "question"}) is None  # no id
    assert PendingInteraction.from_dict({"id": 5}) is None  # non-string id


def test_inject_result_wire_round_trip_drops_web_only_field():
    # to_wire carries exactly the three socket fields; transcript_expected is
    # web-side only and must never cross the socket.
    result = InjectResult(True, reason=None, session_id="s-1", transcript_expected=False)
    assert result.to_wire() == {"accepted": True, "reason": None, "session_id": "s-1"}
    decoded = InjectResult.from_wire(result.to_wire())
    assert decoded == InjectResult(True, None, "s-1")  # transcript_expected back to default


def test_inject_result_from_wire_tolerates_malformed_replies():
    assert InjectResult.from_wire("nope", source="Codex runtime") == InjectResult(
        False, "invalid response from Codex runtime"
    )
    # Non-string reason/session_id degrade to None rather than leaking through.
    assert InjectResult.from_wire({"accepted": 1, "reason": 7, "session_id": []}) == InjectResult(
        True, None, None
    )
