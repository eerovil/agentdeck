from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentdeck.config import AssistantConfig
from agentdeck.deckhand_runner import run_codex_json
from agentdeck.models import Account


class _Process:
    def __init__(self, args, *, result=None, returncode=0):
        self.args = args
        self.result = result
        self.returncode = returncode
        self.pid = 123456

    async def communicate(self, prompt):
        if self.result is not None:
            output = Path(self.args[self.args.index("--output-last-message") + 1])
            output.write_text(self.result)
        return (b"", prompt)


async def test_run_codex_json_uses_selected_schema_model_and_account(tmp_path, monkeypatch):
    captured = {}

    async def create(*args, **kwargs):
        captured.update(args=args, kwargs=kwargs)
        return _Process(args, result='{"title": "ok"}')

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    account = Account("codex:test", "codex", "test", tmp_path / "codex-home")
    schema = tmp_path / "schema.json"
    result = await run_codex_json(
        account,
        AssistantConfig(model="gpt-test"),
        "private transcript",
        schema_path=schema,
        temp_prefix="test-deckhand-",
        job_name="test job",
    )

    assert result == {"title": "ok"}
    assert captured["kwargs"]["env"]["CODEX_HOME"] == str(account.root)
    assert captured["args"][captured["args"].index("--output-schema") + 1] == str(schema)
    assert captured["args"][captured["args"].index("--model") + 1] == "gpt-test"


async def test_run_codex_json_failure_does_not_echo_prompt(tmp_path, monkeypatch):
    async def create(*args, **kwargs):
        return _Process(args, returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    account = Account("codex:test", "codex", "test", tmp_path)

    with pytest.raises(RuntimeError) as error:
        await run_codex_json(
            account,
            AssistantConfig(),
            "secret transcript text",
            schema_path=tmp_path / "schema.json",
            temp_prefix="test-deckhand-",
            job_name="test job",
        )

    assert "secret transcript text" not in str(error.value)
    assert "status 1" in str(error.value)
