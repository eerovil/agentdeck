"""Shared runner for small, ephemeral Deckhand Codex jobs."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import AssistantConfig
    from .models import Account


async def _terminate_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        await process.wait()


async def run_codex_json(
    account: Account,
    config: AssistantConfig,
    prompt: str,
    *,
    schema_path: Path,
    temp_prefix: str,
    job_name: str,
) -> dict[str, Any]:
    """Run one read-only Codex job and return its schema-constrained JSON result."""
    env = os.environ.copy()
    env["CODEX_HOME"] = str(account.root)
    with tempfile.TemporaryDirectory(prefix=temp_prefix) as tmp:
        output = Path(tmp) / "result.json"
        args = [
            "codex",
            "--ask-for-approval",
            "never",
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output),
            "--skip-git-repo-check",
        ]
        if config.model:
            args.extend(("--model", config.model))
        args.append("-")
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(Path.cwd()),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise RuntimeError(f"could not start Codex: {exc}") from exc
        try:
            await asyncio.wait_for(
                process.communicate((prompt + "\n").encode()), timeout=config.timeout_s
            )
        except TimeoutError as exc:
            await _terminate_group(process)
            raise RuntimeError(f"Codex {job_name} timed out") from exc
        except asyncio.CancelledError:
            await _terminate_group(process)
            raise
        if process.returncode != 0:
            # Codex stderr can echo the prompt (including transcript excerpts); keep
            # the operator-visible error generic so chat data never lands in logs.
            raise RuntimeError(
                f"Codex {job_name} exited without an answer (status {process.returncode})"
            )
        try:
            value = json.loads(output.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Codex {job_name} returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"Codex {job_name} returned an invalid result")
        return value
