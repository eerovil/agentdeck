"""history.jsonl → session titles / last prompts.

Each line is one submitted prompt:
{"display": "...", "project": "/path", "sessionId": "...", "timestamp": ...}

Title = first prompt of the session, last_prompt = most recent one.
Malformed lines are skipped, never fatal.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class HistoryEntry:
    session_id: str
    title: str | None = None
    last_prompt: str | None = None
    project: str | None = None


def load_history(config_dir: Path) -> dict[str, HistoryEntry]:
    """Map sessionId → HistoryEntry, in file (chronological) order."""
    path = config_dir / "history.jsonl"
    entries: dict[str, HistoryEntry] = {}
    try:
        text = path.read_text()
    except OSError:
        return entries
    skipped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            sid = str(data["sessionId"])
            display = data.get("display")
        except (ValueError, TypeError, KeyError):
            skipped += 1
            continue
        entry = entries.setdefault(sid, HistoryEntry(session_id=sid))
        if isinstance(display, str) and display:
            if entry.title is None:
                entry.title = display
            entry.last_prompt = display
        if entry.project is None and isinstance(data.get("project"), str):
            entry.project = data["project"]
    if skipped:
        log.debug("history.jsonl: skipped %d malformed lines", skipped)
    return entries
