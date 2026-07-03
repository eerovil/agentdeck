"""sessions/<pid>.json registry parsing + /proc liveness.

Claude Code writes one registry file per process under
$CLAUDE_CONFIG_DIR/sessions/. Files linger after process death, so liveness
must be verified against /proc: the pid is alive *and* /proc/<pid>/stat
field 22 (starttime) matches the recorded ``procStart`` (pid-reuse guard).

Observed schema (CLI v2.1.198), all fields optional for us:
{pid, sessionId, cwd, startedAt, procStart, version, kind, entrypoint}
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

PROC_ROOT = Path("/proc")


@dataclass
class RegistryEntry:
    pid: int
    session_id: str
    cwd: Path | None
    started_at: datetime | None
    proc_start: str | None
    version: str | None
    kind: str | None
    entrypoint: str | None
    raw: dict = field(default_factory=dict, repr=False)


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_registry(config_dir: Path) -> list[RegistryEntry]:
    """Parse all sessions/*.json; malformed files are skipped, never fatal."""
    sessions_dir = config_dir / "sessions"
    entries: list[RegistryEntry] = []
    if not sessions_dir.is_dir():
        return entries
    for path in sorted(sessions_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            pid = int(data["pid"])
            session_id = str(data["sessionId"])
        except (OSError, ValueError, TypeError, KeyError) as exc:
            log.debug("skipping malformed registry file %s: %s", path.name, exc)
            continue
        cwd = data.get("cwd")
        proc_start = data.get("procStart")
        entries.append(
            RegistryEntry(
                pid=pid,
                session_id=session_id,
                cwd=Path(cwd) if isinstance(cwd, str) else None,
                started_at=_parse_ts(data.get("startedAt")),
                proc_start=str(proc_start) if proc_start is not None else None,
                version=data.get("version"),
                kind=data.get("kind"),
                entrypoint=data.get("entrypoint"),
                raw=data,
            )
        )
    return entries


def proc_starttime(pid: int, proc_root: Path = PROC_ROOT) -> str | None:
    """Read /proc/<pid>/stat field 22 (starttime); None if the pid is gone."""
    try:
        stat = (proc_root / str(pid) / "stat").read_text()
    except OSError:
        return None
    # comm (field 2) may contain spaces/parens — split after the last ')'
    _, _, rest = stat.rpartition(")")
    fields = rest.split()
    if len(fields) < 20:  # fields[0] is field 3; starttime (22) is fields[19]
        return None
    return fields[19]


def is_alive(entry: RegistryEntry, proc_root: Path = PROC_ROOT) -> bool:
    """True iff the pid exists AND its starttime matches procStart."""
    starttime = proc_starttime(entry.pid, proc_root)
    if starttime is None:
        return False
    if entry.proc_start is None:
        return False  # can't rule out pid reuse — treat as dead (conservative)
    return starttime == entry.proc_start
