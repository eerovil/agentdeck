"""Antigravity CLI (agy) transcript parsing."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from ...models import TranscriptEvent

log = logging.getLogger(__name__)

_USER_XML_RE = re.compile(
    r"<USER_REQUEST>(.*?)</USER_REQUEST>|"
    r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>|"
    r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>",
    re.DOTALL,
)


def clean_user_content(content: str) -> str:
    """Extract clean prompt text from agy user step content."""
    if not content:
        return ""
    match = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", content, re.DOTALL)
    if match:
        return match.group(1).strip()
    cleaned = _USER_XML_RE.sub("", content).strip()
    return cleaned or content.strip()


def parse_iso_ts(ts_str: str | None) -> datetime | None:
    if not ts_str:
        return None
    try:
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        return datetime.fromisoformat(ts_str).astimezone(UTC)
    except Exception:
        return None


def read_events(transcript_file: Path, after_seq: int = 0) -> list[TranscriptEvent]:
    """Parse JSONL transcript for agy sessions into TranscriptEvents."""
    if not transcript_file.is_file():
        return []

    events: list[TranscriptEvent] = []
    seq = 0

    try:
        with transcript_file.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue

                step_type = data.get("type")
                created_at = data.get("created_at")
                ts = parse_iso_ts(created_at)

                if step_type == "USER_INPUT":
                    seq += 1
                    if seq > after_seq:
                        raw_content = data.get("content", "")
                        text = clean_user_content(raw_content)
                        events.append(
                            TranscriptEvent(
                                seq=seq,
                                role="user",
                                text=text,
                                ts=ts,
                            )
                        )
                elif step_type == "PLANNER_RESPONSE":
                    seq += 1
                    if seq > after_seq:
                        content = data.get("content", "")
                        tool_calls = data.get("tool_calls", [])
                        tool_name = None
                        tool_summary = None
                        tool_detail = None
                        if tool_calls and isinstance(tool_calls, list):
                            tc = tool_calls[0]
                            if isinstance(tc, dict):
                                tool_name = tc.get("name")
                                args = tc.get("args")
                                if isinstance(args, dict):
                                    arg_items = [f"{k}={v}" for k, v in list(args.items())[:2]]
                                    tool_summary = f"{tool_name}({', '.join(arg_items)})"
                                    tool_detail = json.dumps(args, indent=2)

                        events.append(
                            TranscriptEvent(
                                seq=seq,
                                role="assistant",
                                text=content or None,
                                tool_name=tool_name,
                                tool_summary=tool_summary,
                                tool_detail=tool_detail,
                                ts=ts,
                            )
                        )
                elif step_type in (
                    "VIEW_FILE",
                    "RUN_COMMAND",
                    "GREP_SEARCH",
                    "REPLACE_FILE_CONTENT",
                    "WRITE_TO_FILE",
                ):
                    seq += 1
                    if seq > after_seq:
                        content = data.get("content", "")
                        tool_name = step_type.lower()
                        detail = (
                            content[:4000] if isinstance(content, str) and content else None
                        )
                        events.append(
                            TranscriptEvent(
                                seq=seq,
                                role="tool",
                                tool_name=tool_name,
                                tool_summary=f"{step_type} result",
                                tool_detail=detail,
                                ts=ts,
                            )
                        )
    except Exception as err:
        log.warning("Failed to parse agy transcript %s: %s", transcript_file, err)

    return events
