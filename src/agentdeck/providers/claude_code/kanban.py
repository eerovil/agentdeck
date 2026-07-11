"""Kanban dispatch prompts → readable session titles.

The kanban poller spawns headless ``claude -p`` workers whose first user prompt
is a fixed dispatch string, e.g.::

    Run the kanban-worker skill for ScandinavianOutdoor/store#2728.
    Run the kanban-worker-storm skill for protecomp/storm#244 in REVIEW mode: ...

Those workers rarely earn a concise ``aiTitle``, so agentdeck would otherwise
show the raw, near-identical dispatch string as the card title — the one useful
bit (the ``owner/repo#number`` reference) buried at the front and the tail often
truncated. We parse that reference out of the prompt and resolve the real GitHub
issue/PR title via ``gh api`` (disk-cached — titles change rarely), so the card
reads e.g. ``store#2728 · Fix intro text duplication`` instead.

Resolution is best-effort: with no ``gh`` on PATH, no auth, or offline, we fall
back to the bare ``repo#number`` reference and never raise.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# "Run the kanban-worker skill for ScandinavianOutdoor/store#2728."
# "Run the kanban-worker-storm skill for protecomp/storm#244 in REVIEW mode: ..."
_REF_RE = re.compile(
    r"Run the kanban-worker(?:-storm)? skill for "
    r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#(\d+)"
)

# Short mode tags worth surfacing — they distinguish otherwise-identical cards
# (the same issue re-dispatched for review, a merge fix, or a resume).
_MODE_RE = re.compile(r"\b(REVIEW|MERGE-FIX|MERGE-ARM|RESUMING)\b")
_MODE_LABELS = {
    "REVIEW": "review",
    "MERGE-FIX": "merge-fix",
    "MERGE-ARM": "merge-arm",
    "RESUMING": "resume",
}


@dataclass(frozen=True)
class KanbanRef:
    owner: str
    repo: str
    number: int
    mode: str | None = None

    @property
    def key(self) -> str:
        """Cache key — mode-independent (one issue, one title)."""
        return f"{self.owner}/{self.repo}#{self.number}"

    @property
    def short(self) -> str:
        return f"{self.repo}#{self.number}"


def parse_ref(prompt: str | None) -> KanbanRef | None:
    """Extract the kanban issue/PR reference from a dispatch prompt, or None."""
    if not prompt:
        return None
    m = _REF_RE.search(prompt)
    if not m:
        return None
    mode = None
    mm = _MODE_RE.search(prompt)
    if mm:
        mode = _MODE_LABELS.get(mm.group(1))
    return KanbanRef(m.group(1), m.group(2), int(m.group(3)), mode)


def issue_url(ref: KanbanRef) -> str:
    """GitHub URL for the referenced issue/PR. The ``/issues/`` form redirects
    to ``/pull/`` when the number is a PR, so it works for both."""
    return f"https://github.com/{ref.owner}/{ref.repo}/issues/{ref.number}"


def format_title(ref: KanbanRef, issue_title: str | None) -> str:
    """Card title from a ref and its (maybe-unresolved) issue title."""
    base = f"{ref.short} · {issue_title}" if issue_title else ref.short
    if ref.mode:
        base = f"{base} ({ref.mode})"
    return base


# One jq expression pulls the title and everything needed to derive the state
# badge, so a single gh call per ref covers both.
_ISSUE_JQ = (
    "{title, state, state_reason, "
    "is_pr: (.pull_request != null), "
    "merged: (.pull_request.merged_at != null)}"
)


def status_label(rec: dict | None) -> tuple[str, str] | None:
    """(text, kind) badge for a resolved issue/PR record, or None when unknown.

    ``kind`` is a CSS modifier: open (active), merged, done (issue closed as
    completed), dropped (closed as not-planned), closed (PR closed unmerged)."""
    state = (rec or {}).get("state")
    if not state:
        return None
    if state == "open":
        return ("open", "open")
    if rec.get("is_pr"):
        return ("merged", "merged") if rec.get("merged") else ("closed", "closed")
    if rec.get("state_reason") == "not_planned":
        return ("closed", "dropped")
    return ("closed", "done")


class KanbanTitleCache:
    """Disk-backed ``owner/repo#n`` → issue title + GitHub state cache, resolved
    via ``gh api``.

    Held on the (singleton) provider so it persists across scans; mirrored to a
    JSON file so it survives restarts. Titles rarely change, but state does, so
    the refresh TTL is state-aware: open issues re-poll often, terminal ones
    (closed/merged) rarely, and a failed lookup backs off further still.
    """

    # Re-poll an *open* issue's state at most this often (state can change).
    OPEN_TTL_S = 180
    # A closed/merged issue is effectively terminal — re-check rarely.
    TERMINAL_TTL_S = 3600
    # Re-try a failed lookup no sooner than this (avoid hammering on 404/offline).
    NEG_TTL_S = 6 * 3600
    # Per-scan bound on new gh calls so a cold cache can't stall one scan.
    MAX_FETCH_PER_SWEEP = 8
    FETCH_TIMEOUT_S = 6.0

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (Path("~/.cache/agentdeck").expanduser() / "kanban_titles.json")
        self._cache: dict[str, dict] = {}
        self._gh = shutil.which("gh")
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict):
                self._cache = data
        except (OSError, ValueError):
            self._cache = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._cache))
            tmp.replace(self.path)
        except OSError as exc:
            log.debug("kanban cache save failed: %s", exc)

    def get(self, ref: KanbanRef) -> str | None:
        """Cached issue title if known, else None (unresolved or a cached miss)."""
        rec = self._cache.get(ref.key)
        return (rec or {}).get("title") or None

    def get_status(self, ref: KanbanRef) -> tuple[str, str] | None:
        """(text, kind) GitHub-state badge for the ref, or None if unresolved."""
        return status_label(self._cache.get(ref.key))

    def _needs_fetch(self, ref: KanbanRef, now: float) -> bool:
        rec = self._cache.get(ref.key)
        if rec is None:
            return True
        age = now - rec.get("fetched_at", 0.0)
        if not rec.get("title"):  # a cached miss
            return age > self.NEG_TTL_S
        state = rec.get("state")
        if state and state != "open":  # terminal (closed/merged)
            return age > self.TERMINAL_TTL_S
        return age > self.OPEN_TTL_S  # open, or a legacy record with no state yet

    async def resolve_missing(self, refs: list[KanbanRef], now: float) -> bool:
        """Fetch title + state for up to ``MAX_FETCH_PER_SWEEP`` stale/unknown
        refs.

        Returns True if the cache changed (so the caller can re-read it). A
        no-op — returning False — when ``gh`` is absent or nothing is stale.
        """
        if self._gh is None:
            return False
        pending: dict[str, KanbanRef] = {}
        for ref in refs:
            if ref.key not in pending and self._needs_fetch(ref, now):
                pending[ref.key] = ref
        batch = list(pending.values())[: self.MAX_FETCH_PER_SWEEP]
        if not batch:
            return False
        results = await asyncio.gather(*(self._fetch(ref) for ref in batch))
        for ref, rec in results:
            if rec is not None:
                self._cache[ref.key] = {**rec, "fetched_at": now}
            elif (self._cache.get(ref.key) or {}).get("title"):
                # Transient failure — keep the good data, just back off normally.
                self._cache[ref.key]["fetched_at"] = now
            else:
                self._cache[ref.key] = {"fetched_at": now}  # negative cache
        self._save()
        return True

    async def _fetch(self, ref: KanbanRef) -> tuple[KanbanRef, dict | None]:
        assert self._gh is not None
        try:
            proc = await asyncio.create_subprocess_exec(
                self._gh,
                "api",
                f"repos/{ref.owner}/{ref.repo}/issues/{ref.number}",
                "--jq",
                _ISSUE_JQ,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), self.FETCH_TIMEOUT_S)
        except (OSError, TimeoutError) as exc:
            log.debug("gh issue fetch failed for %s: %s", ref.key, exc)
            return (ref, None)
        if proc.returncode != 0:
            return (ref, None)
        try:
            rec = json.loads(out.decode("utf-8", "replace"))
        except ValueError:
            return (ref, None)
        if not isinstance(rec, dict) or not rec.get("title"):
            return (ref, None)
        rec["title"] = str(rec["title"]).strip()
        return (ref, rec)
