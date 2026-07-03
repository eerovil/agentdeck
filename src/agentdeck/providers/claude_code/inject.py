"""Message injection into Claude Code sessions (live or idle).

``claude --resume`` takes no lock and appends atomically to the transcript
whether or not the owning process is alive (verified: no fork, clean append,
O_APPEND lines don't interleave). So the safety rule is NOT "no live process" —
it's "not mid-turn": injecting while a session is actively writing gives two
conversation heads on one transcript. We refuse only while the transcript
changed within ``QUIET_S``; a live-but-quiet session (waiting for input, or a
stuck headless worker) is a fine target. We also require the cwd to exist and be
trusted. No ``--fork-session``, so the reply lands in the same transcript and the
collector's tail picks it up.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from ...models import Account, InjectResult, Session

log = logging.getLogger(__name__)

_MAX_OUTPUT = 2000

# A session whose transcript changed within this many seconds is treated as
# mid-turn — injecting would race the running turn. This, not process existence,
# is the real safety gate.
QUIET_S = 15.0


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
    account: Account,
    session: Session,
    *,
    last_write: datetime | None = None,
    quiet_s: float = QUIET_S,
    now: datetime | None = None,
) -> str | None:
    """Return a refusal reason, or None if it's safe to resume-and-append.

    ``last_write`` is the transcript's current mtime (pass it fresh — don't trust
    cached state). We refuse only while the session is actively writing; whether a
    process is alive is irrelevant to ``--resume`` safety."""
    now = now or datetime.now(UTC)
    if last_write is not None and (now - last_write).total_seconds() < quiet_s:
        return "session is working right now — wait until it pauses, then reply"
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
    last_write: datetime | None = None,
) -> InjectResult:
    if not message.strip():
        return InjectResult(ok=False, detail="empty message")
    reason = preflight(account, session, last_write=last_write)
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


# Sessions with an in-flight background inject turn. A second inject to the same
# session is refused until the first finishes — two writers on one transcript
# corrupts it (belt-and-suspenders alongside the pid-liveness preflight).
_inflight: set[str] = set()
_bg_tasks: set[asyncio.Task] = set()

# How long the HTTP request waits for the turn before handing it to the
# background and telling the user to watch the transcript. A quick turn (or an
# immediate failure) still returns its real result directly; a long one stops
# blocking the page.
GRACE_S = 3.0


async def inject_start(
    account: Account,
    session: Session,
    message: str,
    *,
    timeout_s: float = 600.0,
    claude_bin: str = "claude",
    last_write: datetime | None = None,
) -> InjectResult:
    """Deliver a message without blocking the page for the whole turn.

    Spawns ``claude -p --resume`` and waits up to ``GRACE_S``: a fast turn (or an
    immediate error) returns its real result as before; a longer one is handed to
    a background reaper and we return "streaming", trusting the live transcript
    tail to show the reply as it lands. The child is always drained (an undrained
    full pipe would deadlock it) and never runs two injects on one session at
    once. Appends to the existing session (no ``--fork-session``)."""
    if not message.strip():
        return InjectResult(ok=False, detail="empty message")
    if session.session_id in _inflight:
        return InjectResult(
            ok=False,
            detail="a message is already being delivered to this session — wait for it to finish",
        )
    reason = preflight(account, session, last_write=last_write)
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
            start_new_session=True,
        )
    except (FileNotFoundError, OSError) as exc:
        return InjectResult(ok=False, detail=f"could not launch claude: {exc}")

    _inflight.add(session.session_id)
    comm = asyncio.ensure_future(proc.communicate())
    done, _ = await asyncio.wait({comm}, timeout=GRACE_S)

    if comm not in done:
        # Still running — keep draining in the background, don't block the page.
        task = asyncio.create_task(_reap(session.session_id, proc, comm, timeout_s))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
        return InjectResult(
            ok=True, detail="Sent — the reply is streaming into the transcript below."
        )

    _inflight.discard(session.session_id)
    out, err = comm.result()
    if proc.returncode == 0:
        text = (out or b"").decode(errors="replace").strip()
        return InjectResult(
            ok=True, detail=text[:_MAX_OUTPUT] or "(delivered, no output)", exit_code=0
        )
    detail = (err or b"").decode(errors="replace").strip() or "claude exited non-zero"
    return InjectResult(ok=False, detail=detail[:_MAX_OUTPUT], exit_code=proc.returncode)


async def _reap(
    session_id: str,
    proc: asyncio.subprocess.Process,
    comm: asyncio.Future,
    timeout_s: float,
) -> None:
    """Drain + await a handed-off inject turn; hard-kill if it overruns."""
    try:
        try:
            await asyncio.wait_for(comm, timeout=timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            log.warning("inject turn for %s killed after %.0fs", session_id, timeout_s)
            return
        if proc.returncode not in (0, None):
            _, err = comm.result()
            log.warning(
                "inject turn for %s exited %s: %s",
                session_id,
                proc.returncode,
                (err or b"").decode(errors="replace")[:300],
            )
    finally:
        _inflight.discard(session_id)
