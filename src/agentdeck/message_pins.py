"""Provider-neutral message pinning policy.

Pins address normalized transcript messages by stable ``session_key + seq`` and
store a small display snapshot.  The snapshot keeps an old pin useful when that
event is outside the currently loaded transcript window.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .models import PinnedMessage, TranscriptEvent

MAX_PIN_CONTENT_CHARS = 4_000


def event_is_pinnable(event: TranscriptEvent) -> bool:
    """Only user/assistant messages are pins; tool and liveness rows are not."""
    return event.role in {"user", "assistant"} and bool(
        event.text or event.question or event.answer or event.image_media_types
    )


def pinned_message_from_event(session_key: str, event: TranscriptEvent) -> PinnedMessage:
    if not event_is_pinnable(event):
        raise ValueError("event is not a pinnable message")

    parts = [part.strip() for part in (event.text, event.question, event.answer) if part]
    if event.image_media_types:
        count = len(event.image_media_types)
        parts.append(f"[{count} attached image{'s' if count != 1 else ''}]")
    content = "\n".join(parts).strip()[:MAX_PIN_CONTENT_CHARS]
    return PinnedMessage(
        session_key=session_key,
        seq=event.seq,
        role=event.role,
        content=content,
        event_ts=event.ts,
        pinned_at=datetime.now(UTC),
    )
