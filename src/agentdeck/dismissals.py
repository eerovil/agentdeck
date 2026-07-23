"""Operator dismissals of Deckhand cards, and their durable persistence.

An operator can dismiss a Deckhand card ("done"). A dismissal is valid only
while the session's identity is unchanged, so it auto-reverts:

- an **insight** dismissal reverts when the session's *evidence* signature moves
  (git/PR/state change);
- a **waiting** dismissal — for a chat paused on a pending question — reverts on
  the *message* signature (a new turn), immune to evidence-signature churn.

Both live in one ``assistant_handled`` table, discriminated on disk by a private
sentinel ``kind``. This module owns the two maps, the sentinel, and every table
read/write; ``AssistantService`` supplies session *identities* (the pure
signature functions) but never sees a DB column or the sentinel.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .triage import AssistantInsight

# A waiting dismissal is stored in the same table as an insight dismissal, keyed
# by this reserved ``kind`` so the load can route it to the message-signature
# revert rule. Never leaks outside this module.
_WAITING_DONE_KIND = "__waiting_done__"


@dataclass(frozen=True)
class Dismissal:
    """One operator dismissal of a card. ``insight`` is None for a legacy row
    that carried a signature but no kind/headline (its headline falls back to the
    session title at render time)."""

    signature: str  # the evidence signature captured when the card was dismissed
    insight: AssistantInsight | None


class Dismissals:
    def __init__(self, db=None) -> None:
        self._db = db
        self._insights: dict[str, Dismissal] = {}
        self._waiting: dict[str, str] = {}  # session_key -> message signature

    @classmethod
    def load(cls, db) -> Dismissals:
        store = cls(db)
        handled = db.load_assistant_handled() if db else {}
        for key, (signature, kind, headline, detail) in handled.items():
            if kind == _WAITING_DONE_KIND:
                store._waiting[key] = signature
            elif kind is not None and headline is not None:
                store._insights[key] = Dismissal(
                    signature, AssistantInsight(key, kind, headline, detail or "")
                )
            else:
                store._insights[key] = Dismissal(signature, None)
        return store

    # --- dismiss / restore -------------------------------------------------

    def dismiss_insight(
        self, key: str, signature: str, insight: AssistantInsight
    ) -> None:
        self._insights[key] = Dismissal(signature, insight)
        if self._db:
            self._db.record_assistant_handled(
                key, signature, insight.kind, insight.headline, insight.detail
            )

    def dismiss_waiting(
        self, key: str, message_signature: str, question: str | None
    ) -> None:
        self._waiting[key] = message_signature
        if self._db:
            self._db.record_assistant_handled(
                key, message_signature, _WAITING_DONE_KIND, "Marked done", question
            )

    def is_dismissed(self, key: str) -> bool:
        """Membership only (not validity) — for the undo control."""
        return key in self._insights or key in self._waiting

    def restore(self, key: str) -> Dismissal | None:
        """Undo any dismissal for ``key`` (insight and/or waiting), delete the row,
        and return the removed *insight* Dismissal (None if only waiting / none)."""
        had_waiting = self._waiting.pop(key, None) is not None
        dismissal = self._insights.pop(key, None)
        if (had_waiting or dismissal is not None) and self._db:
            self._db.delete_assistant_handled(key)
        return dismissal

    def drop_insight(self, key: str) -> None:
        """Remove an insight dismissal + its row (delegated prune / evidence moved)."""
        if self._insights.pop(key, None) is not None and self._db:
            self._db.delete_assistant_handled(key)

    def refresh_insight(self, key: str, insight: AssistantInsight) -> None:
        """Re-record a still-valid insight dismissal with the latest card, keeping
        its captured signature."""
        existing = self._insights.get(key)
        if existing is None:
            return
        self._insights[key] = Dismissal(existing.signature, insight)
        if self._db:
            self._db.record_assistant_handled(
                key, existing.signature, insight.kind, insight.headline, insight.detail
            )

    # --- revert maintenance (called from the triage loop) ------------------

    def prune_stale(
        self,
        signatures: Mapping[str, str],
        session_for: Callable[[str], object | None],
        message_signature: Callable[[object], str],
    ) -> None:
        """Drop dismissals whose session identity moved. Insight dismissals key on
        the evidence signature (a key absent from ``signatures`` is left alone —
        not seen, not changed); waiting dismissals on the message signature, and a
        gone/answered question drops the waiting dismissal."""
        for key in list(self._insights):
            new_sig = signatures.get(key)
            if new_sig is not None and new_sig != self._insights[key].signature:
                self.drop_insight(key)
        for key in list(self._waiting):
            session = session_for(key)
            current = (
                message_signature(session)
                if session is not None and getattr(session, "question", None)
                else None
            )
            if current != self._waiting[key]:
                self._waiting.pop(key, None)
                if self._db:
                    self._db.delete_assistant_handled(key)

    # --- queries -----------------------------------------------------------

    def insight_signature(self, key: str) -> str | None:
        dismissal = self._insights.get(key)
        return dismissal.signature if dismissal else None

    def is_waiting_dismissed(self, key: str) -> bool:
        return key in self._waiting

    def insight(self, key: str) -> AssistantInsight | None:
        dismissal = self._insights.get(key)
        return dismissal.insight if dismissal else None

    def active_keys(
        self,
        signatures: Mapping[str, str],
        session_for: Callable[[str], object | None],
        message_signature: Callable[[object], str],
    ) -> frozenset[str]:
        """Sessions currently reading as dismissed — the ``done`` pill. Validity is
        re-checked against current identities so a stale dismissal auto-reverts
        here before the periodic prune even runs. A waiting dismissal whose session
        is gone is kept (absent, not changed)."""
        keys = {
            key
            for key, dismissal in self._insights.items()
            if signatures.get(key, dismissal.signature) == dismissal.signature
        }
        for key, stored in self._waiting.items():
            session = session_for(key)
            current = message_signature(session) if session is not None else stored
            if current == stored:
                keys.add(key)
        return frozenset(keys)

    def latest_insight(self) -> tuple[str, AssistantInsight | None] | None:
        """The most recently dismissed insight (older ones persist as an undo
        stack), or None. Relies on the ordered load / insertion order."""
        for key in reversed(self._insights):
            return (key, self._insights[key].insight)
        return None

    def insight_keys(self) -> list[str]:
        return list(self._insights)
