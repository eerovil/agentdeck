"""Message injection into idle Claude Code sessions.

The one safety rule that must never be violated: **never resume a session whose
owning process is alive** — two writers on one JSONL transcript corrupts it. We
re-check liveness against the registry + /proc at spawn time (not from cached
state), and additionally require the target cwd to exist and be trusted.

Injection appends a turn to the existing session (no ``--fork-session``), so the
reply lands in the same transcript and the collector's tail picks it up.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from ...models import Account, InjectResult, Session
from . import registry as registry_mod

log = logging.getLogger(__name__)

_MAX_OUTPUT = 2000


def is_trusted(config_dir: Path, cwd: Path) -> bool:
    """True iff ``cwd`` has hasTrustDialogAccepted in .claude.json.

    Checks the account's own config file first, then ~/.claude.json (the default
    config dir keeps trust there). We never *write* this file — trusting a
    directory is a decision the user makes by running ``claude`` there.
    """
    for path in (config_dir / ".claude.json", Path.home() / ".claude.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        projects = data.get("projects") if isinstance(data, dict) else None
        if isinstance(projects, dict):
            entry = projects.get(str(cwd))
            if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted"):
                return True
    return False


def preflight(
    account: Account, session: Session, *, proc_root: Path = registry_mod.PROC_ROOT
) -> str | None:
    """Return a refusal reason, or None if injection is safe. Re-reads the
    registry fresh — do NOT trust cached session state here."""
    for e in registry_mod.read_registry(account.root):
        if e.session_id == session.session_id and registry_mod.is_alive(e, proc_root=proc_root):
            return "session is live (a claude process is writing it) — open it in claude.ai instead"
    if session.cwd is None:
        return "session has no known working directory to resume in"
    if not session.cwd.is_dir():
        return f"working directory no longer exists: {session.cwd}"
    if not is_trusted(account.root, session.cwd):
        return (
            f"directory is not trusted — run `claude` once in {session.cwd} "
            "to accept the trust prompt, then retry"
        )
    return None


async def inject_oneshot(
    account: Account,
    session: Session,
    message: str,
    *,
    timeout_s: float = 600.0,
    claude_bin: str = "claude",
    proc_root: Path = registry_mod.PROC_ROOT,
) -> InjectResult:
    if not message.strip():
        return InjectResult(ok=False, detail="empty message")
    reason = preflight(account, session, proc_root=proc_root)
    if reason is not None:
        return InjectResult(ok=False, detail=reason)

    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(account.root)}
    try:
        proc = await asyncio.create_subprocess_exec(
            claude_bin,
            "-p",
            "--resume",
            session.session_id,
            message,
            cwd=str(session.cwd),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        return InjectResult(ok=False, detail=f"could not launch claude: {exc}")

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return InjectResult(ok=False, detail=f"timed out after {timeout_s:.0f}s")

    if proc.returncode == 0:
        text = (out or b"").decode(errors="replace").strip()
        return InjectResult(
            ok=True, detail=text[:_MAX_OUTPUT] or "(delivered, no output)", exit_code=0
        )
    detail = (err or b"").decode(errors="replace").strip() or "claude exited non-zero"
    return InjectResult(ok=False, detail=detail[:_MAX_OUTPUT], exit_code=proc.returncode)
