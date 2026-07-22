"""Unit tests for provider-neutral model policy."""

from __future__ import annotations

import pytest

from agentdeck.models import (
    CONTROL_CAPABILITIES,
    Capability,
    runtime_control_capabilities,
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
