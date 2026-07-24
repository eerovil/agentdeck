#!/usr/bin/env python3
"""AgentDeck kanban poller — the token-free host engine.

Lists open GitHub issues labelled ``agent`` in the configured repo and, for each
one that is not already in flight and has no open PR, creates an isolated git
worktree (with its own ``uv`` venv) and dispatches a **local** AgentDeck worker
to implement it: ``agentdeck delegate`` spins up an AgentDeck-owned Codex chat
whose cwd is that worktree. The worker implements the change, validates, and
opens a PR against the base branch on its own.

Before dispatching work, the poller also fetches the configured base branch and
deploys a new remote revision into the clean live checkout. Web deployment is
immediate; a persistent-runtime restart remains pending until the runtime
reports that every owned Codex and Claude turn is idle.

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
_RUNTIME_PATHS = {
    "pyproject.toml",
    "uv.lock",
    "src/agentdeck/__main__.py",
    "src/agentdeck/config.py",
    "src/agentdeck/models.py",
    "src/agentdeck/runtime.py",
    "src/agentdeck/providers/claude_code/restart.py",
    "src/agentdeck/providers/claude_code/usage.py",
    "src/agentdeck/providers/claude_code/worker.py",
    "src/agentdeck/providers/codex/appserver.py",
    "src/agentdeck/providers/codex/runtime_client.py",
    "systemd/agentdeck-codex.service",
}


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


def by_allowed_author(candidates: list[dict], allowed: list[str] | None) -> list[dict]:
    """Keep only issues opened by an allowed author.

    The repo is public, so an issue's body becomes the worker's instructions —
    only act on issues *we* filed. GitHub logins are case-insensitive. An empty
    or missing allowlist disables the filter (accepts every author); the shipped
    config always sets one, so that fallback only applies to a misconfiguration.
    """
    if not allowed:
        log("warn: no allowed_authors configured — accepting issues from any author")
        return candidates
    allow = {a.lower() for a in allowed}
    kept = []
    for issue in candidates:
        login = ((issue.get("author") or {}).get("login") or "").lower()
        if login in allow:
            kept.append(issue)
    return kept


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


def needs_runtime_restart(paths: list[str]) -> bool:
    """Whether a deploy changes code or configuration loaded by the runtime."""
    return any(path in _RUNTIME_PATHS for path in paths)


# ---------------------------------------------------------------------------
# I/O boundary (gh, git, uv, delegate)
# ---------------------------------------------------------------------------


def _run(
    cmd: list[str], *, cwd: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, check=check, text=True, capture_output=True
    )


def _fetch_base(repo_path: Path, base: str) -> None:
    """Fetch ``origin/<base>``, self-healing past corrupt remote-tracking refs.

    A leftover ``refs/remotes/origin/...`` whose object is missing locally (e.g.
    a merged branch that was pruned remotely and GC'd here) makes *every* fetch
    abort with ``bad object ... does not point to a valid object`` — even a fetch
    of an unrelated branch, because git validates all existing refs in the
    transaction. Deleting the dangling tracking ref lets the fetch proceed; the
    ref re-materializes on the next successful fetch if the branch still exists.
    """
    cmd = ["git", "-C", str(repo_path), "fetch", "origin", base]
    result = _run(cmd, check=False)
    if result.returncode == 0:
        return
    broken = sorted(set(re.findall(r"refs/remotes/\S+", result.stderr)))
    if not broken:
        # Not the corrupt-tracking-ref failure — surface it as before.
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )
    for ref in broken:
        _run(["git", "-C", str(repo_path), "update-ref", "-d", ref], check=False)
        log(f"pruned corrupt remote-tracking ref {ref}")
    _run(cmd)


_BOARD_QUERY = """
query($owner: String!, $name: String!, $label: String!) {
  repository(owner: $owner, name: $name) {
    issues(first: 100, states: OPEN, labels: [$label]) {
      nodes {
        number
        title
        updatedAt
        author { login }
        labels(first: 100) { nodes { name } }
      }
    }
    pullRequests(first: 100, states: OPEN) {
      nodes { number body headRefName }
    }
  }
}
"""


def fetch_board_snapshot(repo: str, label: str) -> tuple[list[dict], list[dict]]:
    """Fetch labelled issues and open PRs in one live GraphQL request."""
    try:
        owner, name = repo.split("/", 1)
    except ValueError as exc:
        raise ValueError(f"invalid GitHub repository {repo!r}") from exc
    out = _run(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-f",
            f"label={label}",
            "-f",
            f"query={_BOARD_QUERY}",
        ]
    ).stdout
    payload = json.loads(out)
    repository = payload["data"]["repository"]
    issues = repository["issues"]["nodes"]
    pulls = repository["pullRequests"]["nodes"]
    if not isinstance(issues, list) or not isinstance(pulls, list):
        raise ValueError("GitHub board snapshot was incomplete")
    for issue in issues:
        labels = issue.get("labels") if isinstance(issue, dict) else None
        if isinstance(labels, dict):
            issue["labels"] = labels.get("nodes", [])
    return (issues, pulls)


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


def load_deploy_state(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        key: value
        for key, value in raw.items()
        if key in {"web_sha", "runtime_sha"} and isinstance(value, str)
    }


def save_deploy_state(path: Path, state: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


def _revision(repo_path: Path, ref: str) -> str:
    return _run(["git", "-C", str(repo_path), "rev-parse", ref]).stdout.strip()


def _changed_paths(repo_path: Path, old: str, new: str) -> list[str]:
    if old == new:
        return []
    out = _run(
        ["git", "-C", str(repo_path), "diff", "--name-only", f"{old}..{new}"]
    ).stdout
    return [line for line in out.splitlines() if line]


def _service_pid(service: str) -> str:
    return _run(
        ["systemctl", "--user", "show", service, "--property", "MainPID", "--value"]
    ).stdout.strip()


def _runtime_activity(socket_path: str) -> dict | None:
    try:
        out = _run(
            [
                "curl",
                "-fsS",
                "--max-time",
                "6",
                "--unix-socket",
                socket_path,
                "http://localhost/activity",
            ]
        ).stdout
        payload = json.loads(out)
    except (subprocess.CalledProcessError, ValueError):
        return None
    valid = isinstance(payload, dict) and isinstance(payload.get("active"), bool)
    return payload if valid else None


def _wait_for(check, attempts: int = 10) -> bool:
    for attempt in range(attempts):
        if check():
            return True
        if attempt + 1 < attempts:
            time.sleep(0.5)
    return False


def _restart_web(deploy_cfg: dict) -> None:
    service = deploy_cfg["web_service"]
    before = _service_pid(service)
    _run(["systemctl", "--user", "restart", service])
    healthy = _wait_for(
        lambda: _run(
            ["curl", "-fsS", "--max-time", "6", deploy_cfg["health_url"]],
            check=False,
        ).returncode
        == 0
    )
    after = _service_pid(service)
    if not healthy or after in {"", "0"} or after == before:
        raise RuntimeError(f"{service} restart did not produce a new healthy process")


def _restart_runtime(deploy_cfg: dict) -> None:
    service = deploy_cfg["runtime_service"]
    before = _service_pid(service)
    _run(["systemctl", "--user", "restart", service])
    healthy = _wait_for(lambda: _runtime_activity(deploy_cfg["runtime_socket"]) is not None)
    after = _service_pid(service)
    if not healthy or after in {"", "0"} or after == before:
        raise RuntimeError(f"{service} restart did not produce a new healthy process")


def autodeploy(cfg: dict, *, dry_run: bool = False) -> None:
    """Fast-forward and deploy a newly observed remote base-branch revision.

    Web and runtime revisions are recorded separately so a failed health check
    is retried and a runtime restart can remain pending while any turn is active.
    """
    deploy_cfg = cfg.get("autodeploy") or {}
    if not deploy_cfg.get("enabled"):
        return

    repo_path = Path(cfg["repo_path"])
    base = cfg["base_branch"]
    remote_ref = f"origin/{base}"
    state_path = Path(deploy_cfg["state_path"])

    _fetch_base(repo_path, base)
    branch = _run(
        ["git", "-C", str(repo_path), "rev-parse", "--abbrev-ref", "HEAD"]
    ).stdout.strip()
    dirty = _run(
        ["git", "-C", str(repo_path), "status", "--porcelain"]
    ).stdout.strip()
    if branch != base or dirty:
        log(f"autodeploy deferred: live checkout must be clean on {base}")
        return

    local_sha = _revision(repo_path, "HEAD")
    target_sha = _revision(repo_path, remote_ref)
    state = load_deploy_state(state_path)
    if not state:
        state = {"web_sha": local_sha, "runtime_sha": local_sha}
        if not dry_run:
            save_deploy_state(state_path, state)

    if local_sha != target_sha:
        ancestor = (
            _run(
                [
                    "git",
                    "-C",
                    str(repo_path),
                    "merge-base",
                    "--is-ancestor",
                    local_sha,
                    target_sha,
                ],
                check=False,
            ).returncode
            == 0
        )
        if not ancestor:
            log("autodeploy deferred: origin cannot fast-forward the live checkout")
            return
        if dry_run:
            log(f"DRY: would fast-forward {base} to {target_sha[:12]}")
        else:
            _run(["git", "-C", str(repo_path), "merge", "--ff-only", target_sha])

    web_sha = state.get("web_sha", local_sha)
    runtime_sha = state.get("runtime_sha", local_sha)
    if dry_run:
        if web_sha != target_sha:
            log(f"DRY: would deploy web at {target_sha[:12]}")
        if runtime_sha != target_sha and needs_runtime_restart(
            _changed_paths(repo_path, runtime_sha, target_sha)
        ):
            log(f"DRY: would restart runtime at {target_sha[:12]} once all chats are idle")
        return

    if web_sha != target_sha:
        _run(["uv", "sync"], cwd=repo_path)
        _restart_web(deploy_cfg)
        state["web_sha"] = target_sha
        save_deploy_state(state_path, state)
        log(f"deployed web at {target_sha[:12]}")

    if runtime_sha == target_sha:
        return
    changed = _changed_paths(repo_path, runtime_sha, target_sha)
    if not needs_runtime_restart(changed):
        state["runtime_sha"] = target_sha
        save_deploy_state(state_path, state)
        return

    activity = _runtime_activity(deploy_cfg["runtime_socket"])
    if activity is None:
        log("runtime restart deferred: activity endpoint unavailable")
        return
    if activity["active"]:
        turns = activity.get("active_turns", {})
        log(f"runtime restart deferred: active chats remain ({turns})")
        return
    _restart_runtime(deploy_cfg)
    state["runtime_sha"] = target_sha
    save_deploy_state(state_path, state)
    log(f"deployed runtime at {target_sha[:12]}")


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
        _fetch_base(repo_path, base)
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
    # `--approval-policy never` is what makes the worker autonomous: Codex would
    # otherwise prompt (and block) on commands like git rebase / push / gh pr
    # create. Network is already enabled for delegations, so with never + a
    # workspace-write sandbox scoped to the worktree it runs unattended.
    cmd = (
        f"{shlex.quote(cfg['agentdeck_bin'])} delegate "
        f"--cwd {shlex.quote(str(worktree))} --sandbox workspace-write "
        f"--approval-policy never "
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

    try:
        autodeploy(cfg, dry_run=args.dry_run)
    except (KeyError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        log(f"autodeploy failed; will retry next poll: {exc}")

    state = load_state(state_path)
    # Public repo: only ever act on issues WE opened (their body is the worker's
    # prompt). Filter by author before anything else touches them.
    issues, open_prs = fetch_board_snapshot(cfg["repo"], cfg["label"])
    candidates = by_allowed_author(issues, cfg.get("allowed_authors"))
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
