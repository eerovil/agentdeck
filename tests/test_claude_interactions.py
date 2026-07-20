"""Provider mapping: worker can_use_tool control_request -> PendingInteraction."""

from __future__ import annotations

from agentdeck.models import Session, SessionStatus
from agentdeck.providers.claude_code.provider import _interaction_from_control_request


def _session() -> Session:
    return Session(
        key="claude_code:test:sid-1",
        account_key="claude_code:test",
        session_id="sid-1",
        status=SessionStatus.IDLE,
    )


def test_maps_askuserquestion_to_question_interaction():
    raw = {
        "request_id": "req-1",
        "tool_name": "AskUserQuestion",
        "display_name": "AskUserQuestion",
        "input": {
            "questions": [
                {
                    "question": "Pick a fruit",
                    "header": "Fruit",
                    "multiSelect": True,
                    "options": [
                        {"label": "apple", "description": "a"},
                        {"label": "banana", "description": "b"},
                    ],
                }
            ]
        },
    }
    interaction = _interaction_from_control_request(raw, _session())
    assert interaction.id == "req-1"
    assert interaction.kind == "question"
    assert interaction.thread_id == "sid-1"
    q = interaction.questions[0]
    assert q.id == "0" and q.prompt == "Pick a fruit" and q.header == "Fruit"
    assert q.multiselect is True and q.allow_other is True
    assert [o.label for o in q.options] == ["apple", "banana"]


def test_maps_permission_gate_to_permission_interaction():
    raw = {
        "request_id": "g1",
        "tool_name": "Bash",
        "display_name": "Bash",
        "description": "delete a file",
        "input": {"command": "rm x"},
    }
    interaction = _interaction_from_control_request(raw, _session())
    assert interaction.kind == "permission"
    assert interaction.title == "Allow Bash?"
    assert interaction.message == "delete a file"
    assert interaction.command == "rm x"
    assert interaction.questions == ()
    assert "cancel" in interaction.decisions


def test_returns_none_without_request_id():
    assert _interaction_from_control_request({"tool_name": "Bash"}, _session()) is None
