"""Agent-triggered runtime restart with autonomous continuation (issue #20).

The persistent runtime unit uses ``KillMode=control-group``. An agent worker and
the bash it spawns both live in that cgroup, so a plain ``systemctl restart`` of
the runtime tears the agent down mid-turn — its own restart command is killed
before it returns. This module lets an agent restart the runtime and keep going:

* :func:`trigger_detached_restart` runs the restart from a *transient* systemd
  scope (its own cgroup) via ``systemd-run``, so the runtime's cgroup teardown
  does not kill the restart job.
* Before triggering, the agent leaves a durable :class:`RestartMarker` in the
  Claude-worker state dir (which survives the restart). When the fresh runtime
  boots it reads the marker and delivers a follow-up turn to the agent's session
  (``claude --resume``), so the agent continues where it left off.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

MARKER_DIRNAME = "restart-continue"

DEFAULT_CONTINUATION = (
    "The runtime has been restarted and your new code is now live. "
    "Continue the task you were working on before the restart."
)

# Markers older than this are ignored on boot: a marker left behind by a crash
# loop (rather than an intended restart) must not keep re-injecting forever.
MARKER_TTL_S = 900.0


@dataclass
class RestartMarker:
    """A pending "resume this session after the runtime restarts" instruction."""

    session_id: str
    prompt: str
    service: str
    created_at: float

    @property
    def delivery_id(self) -> str:
        """Stable at-most-once identity for this marker's continuation turn."""
        return f"restart-continue:{self.session_id}:{int(self.created_at)}"

    def is_stale(self, now: float) -> bool:
        """Whether this marker has exceeded the restart continuation window."""
        return now - self.created_at > MARKER_TTL_S

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "prompt": self.prompt,
            "service": self.service,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> RestartMarker:
        return cls(
            session_id=str(raw["session_id"]),
            prompt=str(raw["prompt"]),
            service=str(raw.get("service", "")),
            created_at=float(raw["created_at"]),
        )


def markers_dir(state_dir: Path) -> Path:
    return Path(state_dir) / MARKER_DIRNAME


def _marker_path(state_dir: Path, session_id: str) -> Path:
    # Session ids are opaque; keep the on-disk filename filesystem-safe.
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id)
    return markers_dir(state_dir) / f"{safe}.json"


def write_marker(state_dir: Path, marker: RestartMarker) -> Path:
    path = _marker_path(state_dir, marker.session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(marker.to_dict(), indent=1))
    tmp.replace(path)
    return path


def read_markers(state_dir: Path) -> list[tuple[Path, RestartMarker]]:
    """Load all markers; drop (and delete) any that are malformed."""
    directory = markers_dir(state_dir)
    entries: list[tuple[Path, RestartMarker]] = []
    try:
        files = sorted(directory.glob("*.json"))
    except OSError:
        return entries
    for path in files:
        try:
            marker = RestartMarker.from_dict(json.loads(path.read_text()))
        except (OSError, ValueError, KeyError, TypeError):
            path.unlink(missing_ok=True)
            continue
        entries.append((path, marker))
    return entries


def _unit_from_cgroup(text: str) -> str | None:
    """Return the deepest ``*.service`` in a cgroup-v2 membership line, if any."""
    services = re.findall(r"[A-Za-z0-9@:._-]+\.service", text)
    return services[-1] if services else None


def detect_runtime_unit() -> str | None:
    """Best-effort: the systemd unit this process runs under (cgroup-v2 leaf)."""
    try:
        text = Path("/proc/self/cgroup").read_text()
    except OSError:
        return None
    return _unit_from_cgroup(text)


def looks_like_runtime_unit(service: str) -> bool:
    name = service.lower()
    return "agentdeck" in name and "codex" in name


def trigger_detached_restart(service: str) -> None:
    """Restart ``service`` from a transient scope that outlives its cgroup.

    ``systemd-run --user --collect`` starts a throwaway unit in its own cgroup;
    it runs ``systemctl --user restart <service>`` and is reaped on completion,
    so tearing down the runtime cgroup does not kill the restart job.
    """
    subprocess.run(
        [
            "systemd-run",
            "--user",
            "--collect",
            "--quiet",
            "systemctl",
            "--user",
            "restart",
            service,
        ],
        check=True,
    )
