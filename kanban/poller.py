#!/usr/bin/env python3
"""AgentDeck kanban poller — the token-free host engine.

Lists open GitHub issues labelled ``agent`` in the configured repo and, for each
one that is not already in flight and has no open PR, creates an isolated git
worktree (with its own ``uv`` venv) and dispatches a **local** AgentDeck worker
to implement it: ``agentdeck delegate`` spins up an AgentDeck-owned Codex chat
whose cwd is that worktree. The worker implements the change, validates, and
opens a PR against the base branch on its own.

No LLM inference happens in this process — it is plain ``gh`` + ``git`` + ``uv``.
The only token spend is the dispatched workers, and only when there is real work.

Idempotency is a JSON **state file** plus a **stale gate**: an issue dispatched
less than ``stale_hours`` ago is considered in flight and skipped; a worker that
died leaves a stale entry that is eligible for re-dispatch after the gate. An
issue that already has an open PR (linked via ``Closes #N`` or an
``agent/issue-N-*`` head branch) is never re-dispatched.

Usage:
    poller.py [--config PATH] [--dry-run] [--verbose]

``--dry-run`` performs every read (issues, PRs, state) and prints what it *would*
dispatch, without creating worktrees or spawning workers.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# `Closes #12`, `fixes #3`, `resolved #99` — GitHub's issue-closing keywords.
_CLOSES_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.IGNORECASE
)
_BRANCH_ISSUE_RE = re.compile(r"issue-(\d+)")


def log(msg: str) -> None:
    print(f"[kanban] {msg}", file=sys.stderr, flush=True)


def load_config(path: str | os.PathLike | None = None) -> dict:
    p = Path(path or os.environ.get("AGENTDECK_KANBAN_CONFIG") or HERE / "config.json")
    return json.loads(p.read_text())


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested — no I/O, no side effects)
# ---------------------------------------------------------------------------


def slugify(title: str, maxlen: int = 40) -> str:
    """A filesystem/branch-safe slug from an issue title."""
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s[:maxlen].rstrip("-") or "issue"


def prd_issue_numbers(open_prs: list[dict]) -> set[int]:
    """Issue numbers that already have an open PR — matched by a closing keyword
    in the PR body or by an ``agent/issue-<n>-*`` head branch."""
    nums: set[int] = set()
    for pr in open_prs:
        for m in _CLOSES_RE.finditer(pr.get("body") or ""):
            nums.add(int(m.group(1)))
        m = _BRANCH_ISSUE_RE.search(pr.get("headRefName") or "")
        if m:
            nums.add(int(m.group(1)))
    return nums


def _in_flight(entry: dict, now: float, stale_seconds: float) -> bool:
    return (
        entry.get("status") == "working"
        and (now - float(entry.get("dispatched_at", 0))) < stale_seconds
    )


def select_issues(
    candidates: list[dict],
    state: dict,
    prd_numbers: set[int],
    *,
    now: float,
    cap: int,
    stale_seconds: float,
) -> list[dict]:
    """Decide which issues to dispatch this run.

    - never exceed ``cap`` concurrent in-flight workers;
    - skip issues with an open PR (``prd_numbers``);
    - skip issues finished (state ``done``) or still in flight (dispatched within
      the stale gate); a stale ``working`` entry is eligible again;
    - oldest issue number first, deterministic.
    """
    in_flight_count = sum(1 for e in state.values() if _in_flight(e, now, stale_seconds))
    slots = max(0, cap - in_flight_count)
    if slots == 0:
        return []
    picked: list[dict] = []
    for issue in sorted(candidates, key=lambda i: i["number"]):
        n = issue["number"]
        if n in prd_numbers:
            continue
        entry = state.get(str(n))
        if entry:
            if entry.get("status") == "done":
                continue
            if _in_flight(entry, now, stale_seconds):
                continue
        picked.append(issue)
        if len(picked) >= slots:
            break
    return picked


# ---------------------------------------------------------------------------
# I/O boundary (gh, git, uv, delegate)
# ---------------------------------------------------------------------------


def _run(
    cmd: list[str], *, cwd: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, check=check, text=True, capture_output=True
    )


def list_agent_issues(repo: str, label: str) -> list[dict]:
    out = _run(
        [
            "gh", "issue", "list", "-R", repo, "--label", label, "--state", "open",
            "--limit", "100", "--json", "number,title,labels,updatedAt",
        ]
    ).stdout
    return json.loads(out or "[]")


def list_open_prs(repo: str) -> list[dict]:
    out = _run(
        [
            "gh", "pr", "list", "-R", repo, "--state", "open",
            "--limit", "100", "--json", "number,body,headRefName",
        ]
    ).stdout
    return json.loads(out or "[]")


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text()).get("issues", {})
    except (OSError, ValueError):
        return {}


def save_state(path: Path, issues: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"issues": issues}, indent=2, sort_keys=True))
    tmp.replace(path)


def ensure_worktrees_ignored(repo_path: Path, worktrees_root: str) -> None:
    exclude = repo_path / ".git" / "info" / "exclude"
    line = f"{worktrees_root}/"
    try:
        existing = exclude.read_text() if exclude.exists() else ""
        if line not in existing.split():
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with exclude.open("a") as fh:
                fh.write(("" if existing.endswith("\n") or not existing else "\n") + line + "\n")
    except OSError as exc:
        log(f"warn: could not update .git/info/exclude: {exc}")


def create_worktree(repo_path: Path, cfg: dict, number: int, branch: str) -> Path:
    """Create (or reuse) an isolated worktree at ``<root>/issue-<n>`` with its own
    uv venv, branched from a fresh ``origin/<base_branch>``."""
    ensure_worktrees_ignored(repo_path, cfg["worktrees_root"])
    wt = repo_path / cfg["worktrees_root"] / f"issue-{number}"
    base = cfg["base_branch"]
    if not wt.exists():
        _run(["git", "-C", str(repo_path), "fetch", "origin", base])
        # Reuse the branch if a prior (stale) attempt created it; else start fresh.
        exists = _run(
            ["git", "-C", str(repo_path), "rev-parse", "--verify", "--quiet", branch],
            check=False,
        ).returncode == 0
        if exists:
            _run(["git", "-C", str(repo_path), "worktree", "add", str(wt), branch])
        else:
            _run(
                ["git", "-C", str(repo_path), "worktree", "add", str(wt), "-b", branch,
                 f"origin/{base}"]
            )
    # Each worktree gets its own venv so parallel test/Playwright runs don't share
    # one interpreter; the app tests are in-process (no port/DB), so this is safe.
    _run(["uv", "sync"], cwd=wt)
    return wt


def build_prompt(cfg: dict, issue: dict, worktree: Path, branch: str) -> str:
    template = (HERE / "worker_prompt.md").read_text()
    return template.format(
        repo=cfg["repo"],
        number=issue["number"],
        title=issue["title"],
        branch=branch,
        base_branch=cfg["base_branch"],
        worktree=str(worktree),
        kanban_dir=str(cfg.get("kanban_dir", HERE)),
        shot=str(Path(cfg.get("kanban_dir", HERE)) / "shot.py"),
    )


def dispatch(cfg: dict, worktree: Path, prompt: str) -> None:
    """Fire a detached local delegation. ``agentdeck delegate`` posts to the
    running AgentDeck, which owns the Codex worker chat server-side; the CLI just
    babysits + logs, so we background it and return immediately."""
    prompt_file = worktree / ".kanban" / "prompt.md"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(prompt)
    cmd = (
        f"{shlex.quote(cfg['agentdeck_bin'])} delegate "
        f"--cwd {shlex.quote(str(worktree))} --sandbox workspace-write "
        f"< {shlex.quote(str(prompt_file))} >> {shlex.quote(cfg['log_path'])} 2>&1"
    )
    subprocess.Popen(
        ["bash", "-lc", cmd],
        start_new_session=True,
        env={**os.environ, "AGENTDECK_URL": cfg["agentdeck_url"]},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="poller", description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    cfg.setdefault("kanban_dir", str(HERE))
    repo_path = Path(cfg["repo_path"])
    state_path = Path(cfg["state_path"])
    stale_seconds = float(cfg["stale_hours"]) * 3600
    now = time.time()

    state = load_state(state_path)
    candidates = list_agent_issues(cfg["repo"], cfg["label"])
    open_prs = list_open_prs(cfg["repo"])
    prd = prd_issue_numbers(open_prs)

    # An issue whose PR is now open graduates to `done` so it's never re-picked.
    for pr_num in prd:
        entry = state.get(str(pr_num))
        if entry and entry.get("status") != "done":
            entry["status"] = "done"
    picked = select_issues(
        candidates, state, prd, now=now, cap=int(cfg["concurrency"]), stale_seconds=stale_seconds
    )

    if args.verbose or args.dry_run:
        log(
            f"{len(candidates)} agent issue(s), {len(prd)} with open PR, "
            f"{sum(1 for e in state.values() if _in_flight(e, now, stale_seconds))} in flight "
            f"→ {len(picked)} to dispatch"
        )
    if not picked:
        if not args.dry_run:
            save_state(state_path, state)
        return 0

    for issue in picked:
        number = issue["number"]
        branch = f"{cfg['branch_prefix']}{number}-{slugify(issue['title'])}"
        if args.dry_run:
            log(f"DRY: would dispatch #{number} on {branch}")
            continue
        try:
            worktree = create_worktree(repo_path, cfg, number, branch)
            dispatch(cfg, worktree, build_prompt(cfg, issue, worktree, branch))
        except subprocess.CalledProcessError as exc:
            log(f"dispatch #{number} failed: {exc.stderr or exc}")
            continue
        state[str(number)] = {
            "status": "working",
            "dispatched_at": now,
            "branch": branch,
            "title": issue["title"],
        }
        save_state(state_path, state)
        log(f"dispatched #{number} → {branch}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
