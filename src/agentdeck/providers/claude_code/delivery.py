"""At-most-once delivery receipts for deck-owned Claude workers.

The persistent runtime guarantees each owned-worker delivery is applied at most
once, keyed by a stable ``delivery_id`` (AGENTS.md). This module owns that
guarantee as one value object over a worker's durable receipt dict: it hides the
fingerprint recipe, the prepared-vs-finalized record shapes, age/count pruning,
and the uncertain-replay rule behind a narrow interface.

It WRAPS the durable dict rather than owning it, so the on-disk state format
(``WorkerRecord.deliveries``, serialized by the host) stays byte-identical across
runtime restarts and rollbacks. The caller persists the dict after
``prepare``/``finalize``/``forget`` — the persist-before-write ordering is the
caller's to control, since only it knows when the child write happens.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

# Receipts are retained by age (not a fixed FIFO count) so a delivery ID retried
# after many intervening deliveries still finds its record and dedups, instead of
# silently re-executing. The high count cap is only a runaway backstop.
DELIVERY_RECEIPT_TTL_S = 24 * 3600.0
DELIVERY_RECEIPT_CAP = 4096


@dataclass
class DeliverResult:
    accepted: bool
    action: str  # spawned/revived/steered/queued/parked/released/stopped/rejected
    reason: str | None = None
    session_id: str | None = None


class DeliveryReceipts:
    """The at-most-once fence for one worker key, over its durable receipt dict."""

    def __init__(self, store: dict[str, dict[str, Any]]) -> None:
        self._store = store

    @staticmethod
    def fingerprint(
        message: str,
        *,
        cwd: str | None,
        fresh: bool,
        images: list[str],
        model: str | None,
        permission_mode: str | None,
    ) -> str:
        payload = json.dumps(
            {
                "message": message,
                "cwd": cwd,
                "fresh": fresh,
                # Uploaded images land at a fresh random path on every request
                # (uploads.py: mkdtemp + token_hex), so the *paths* differ across a
                # legitimate retry of the same logical delivery and would falsely
                # trip delivery_id_conflict. Fingerprint the stable count instead.
                "images": len(images),
                "model": model,
                "permission_mode": permission_mode,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def lookup(
        self, delivery_id: str | None, fingerprint: str, *, live_session_id: str | None
    ) -> DeliverResult | None:
        """The cached outcome for ``delivery_id``, or None to deliver normally.

        None → no id or no receipt. A fingerprint mismatch is a
        ``delivery_id_conflict`` rejection. A finalized receipt (carries an
        ``action``) replays its original result. A prepared-only receipt means a
        prior attempt persisted the fence and then wrote — or was killed around
        the write — without confirming; at-most-once forbids writing again, so it
        surfaces as ``uncertain``, preferring the live session id (frozen None for
        a fresh spawn) so the replay still points at a resumable session.
        """
        if not delivery_id:
            return None
        receipt = self._store.get(delivery_id)
        if receipt is None:
            return None
        if receipt.get("fingerprint") != fingerprint:
            return DeliverResult(False, "rejected", reason="delivery_id_conflict")
        action = receipt.get("action")
        if action:
            return DeliverResult(True, str(action), session_id=receipt.get("session_id"))
        return DeliverResult(
            True, "uncertain", session_id=live_session_id or receipt.get("session_id")
        )

    def prepare(
        self, delivery_id: str | None, fingerprint: str, *, session_id: str | None
    ) -> None:
        """Arm the fence with a durable prepared receipt. The caller MUST persist
        the store before writing to the child, so a crash in the write→confirm
        window replays as uncertain instead of re-delivering."""
        if not delivery_id:
            return
        self._store[delivery_id] = {
            "fingerprint": fingerprint,
            "session_id": session_id,
            "at": time.time(),
        }
        self._prune()

    def finalize(
        self, delivery_id: str | None, fingerprint: str, result: DeliverResult
    ) -> None:
        """Clear the fence into a replayable finalized receipt. Caller persists."""
        if not delivery_id:
            return
        self._store[delivery_id] = {
            "fingerprint": fingerprint,
            "action": result.action,
            "session_id": result.session_id,
            "at": time.time(),
        }
        self._prune()

    def forget(self, delivery_id: str | None, *, write_started: bool) -> bool:
        """Drop a prepared receipt whose write CLEANLY failed (nothing sent), so
        the same id may be retried. Refuses — keeps the fence, returns False —
        when ``write_started`` is True: an uncertain write must never be re-armed
        as a fresh send. Returns True when a receipt was dropped (caller persists).
        """
        if write_started or not delivery_id:
            return False
        return self._store.pop(delivery_id, None) is not None

    def _prune(self) -> None:
        """Drop receipts by age, with a high count backstop — monotonic, not a
        small FIFO, so a delivery ID retried after many intervening deliveries
        still finds its record and dedups instead of silently re-executing."""
        now = time.time()
        for did in [
            d
            for d, r in self._store.items()
            if now - float(r.get("at", 0.0)) > DELIVERY_RECEIPT_TTL_S
        ]:
            self._store.pop(did, None)
        if len(self._store) > DELIVERY_RECEIPT_CAP:
            for did in sorted(
                self._store, key=lambda d: float(self._store[d].get("at", 0.0))
            )[: len(self._store) - DELIVERY_RECEIPT_CAP]:
                self._store.pop(did, None)
