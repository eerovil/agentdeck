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


class KanbanTitleCache:
    """Disk-backed ``owner/repo#n`` → issue-title cache resolved via ``gh api``.

    Held on the (singleton) provider so it persists across scans; mirrored to a
    JSON file so it survives restarts. Both hits and misses are cached, on
    separate TTLs, so a renamed issue eventually refreshes and a 404/offline
    lookup isn't retried on every scan.
    """

    # Re-resolve a known title at most this often (issues get renamed rarely).
    TTL_S = 7 * 24 * 3600
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

    def _needs_fetch(self, ref: KanbanRef, now: float) -> bool:
        rec = self._cache.get(ref.key)
        if rec is None:
            return True
        age = now - rec.get("fetched_at", 0.0)
        return age > (self.TTL_S if rec.get("title") else self.NEG_TTL_S)

    async def resolve_missing(self, refs: list[KanbanRef], now: float) -> bool:
        """Fetch titles for up to ``MAX_FETCH_PER_SWEEP`` stale/unknown refs.

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
        for ref, title in results:
            self._cache[ref.key] = {"title": title, "fetched_at": now}
        self._save()
        return True

    async def _fetch(self, ref: KanbanRef) -> tuple[KanbanRef, str | None]:
        assert self._gh is not None
        try:
            proc = await asyncio.create_subprocess_exec(
                self._gh,
                "api",
                f"repos/{ref.owner}/{ref.repo}/issues/{ref.number}",
                "--jq",
                ".title",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), self.FETCH_TIMEOUT_S)
        except (OSError, TimeoutError) as exc:
            log.debug("gh title fetch failed for %s: %s", ref.key, exc)
            return (ref, None)
        if proc.returncode != 0:
            return (ref, None)
        title = out.decode("utf-8", "replace").strip()
        return (ref, title or None)
