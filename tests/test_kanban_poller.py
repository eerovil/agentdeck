"""Unit tests for the kanban poller's deterministic core (kanban/poller.py).

These cover the selection/idempotency logic — the part that decides what gets
dispatched — with no gh/git/uv/delegate I/O. The side-effecting wrappers
(create_worktree, dispatch) are exercised live during rollout, not here.
"""

import importlib.util
import subprocess
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "kanban_poller", Path(__file__).parents[1] / "kanban" / "poller.py"
)
poller = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(poller)


HOUR = 3600.0
STALE = 2 * HOUR


def issue(n, title="A thing"):
    return {"number": n, "title": title}


def test_slugify_is_branch_safe():
    assert poller.slugify("Fix the Parent/Subagent bug!") == "fix-the-parent-subagent-bug"
    assert poller.slugify("   ") == "issue"
    assert len(poller.slugify("x" * 200)) <= 40
    assert not poller.slugify("Trailing punctuation ...").endswith("-")


def _issue_by(n, login):
    return {"number": n, "title": "t", "author": ({"login": login} if login is not None else None)}


def test_by_allowed_author_keeps_only_allowlisted_logins():
    cands = [_issue_by(1, "eerovil"), _issue_by(2, "stranger"), _issue_by(3, "EEROVIL")]
    kept = poller.by_allowed_author(cands, ["eerovil"])
    # Case-insensitive match; the stranger's issue is dropped.
    assert [i["number"] for i in kept] == [1, 3]


def test_by_allowed_author_drops_missing_author():
    cands = [_issue_by(1, None), {"number": 2, "title": "t"}, _issue_by(3, "eerovil")]
    assert [i["number"] for i in poller.by_allowed_author(cands, ["eerovil"])] == [3]


def test_by_allowed_author_empty_allowlist_accepts_all():
    cands = [_issue_by(1, "anyone")]
    assert poller.by_allowed_author(cands, []) == cands
    assert poller.by_allowed_author(cands, None) == cands


def test_prd_issue_numbers_from_body_and_branch():
    prs = [
        {"body": "Closes #12\n\ndetails", "headRefName": "agent/issue-12-foo"},
        {"body": "unrelated", "headRefName": "agent/issue-7-bar"},
        {"body": "fixes #9 and Resolved #10", "headRefName": "feature/x"},
    ]
    assert poller.prd_issue_numbers(prs) == {12, 7, 9, 10}


def test_select_skips_prd_done_and_inflight():
    now = 1000 * HOUR
    candidates = [issue(1), issue(2), issue(3), issue(4)]
    state = {
        "2": {"status": "done"},
        "3": {"status": "working", "dispatched_at": now - HOUR},  # in flight
    }
    picked = poller.select_issues(
        candidates, state, {1}, now=now, cap=5, stale_seconds=STALE
    )
    # #1 has an open PR, #2 done, #3 in flight → only #4 remains.
    assert [i["number"] for i in picked] == [4]


def test_stale_worker_is_eligible_again():
    now = 1000 * HOUR
    state = {"5": {"status": "working", "dispatched_at": now - (STALE + 60)}}
    picked = poller.select_issues(
        [issue(5)], state, set(), now=now, cap=2, stale_seconds=STALE
    )
    assert [i["number"] for i in picked] == [5]


def test_cap_counts_only_live_inflight_and_limits_dispatch():
    now = 1000 * HOUR
    candidates = [issue(n) for n in (10, 11, 12, 13)]
    state = {
        "99": {"status": "working", "dispatched_at": now - HOUR},  # 1 live in flight
        "98": {"status": "working", "dispatched_at": now - (STALE + 1)},  # stale, doesn't count
    }
    picked = poller.select_issues(
        candidates, state, set(), now=now, cap=2, stale_seconds=STALE
    )
    # cap 2 minus 1 live in-flight = 1 slot, lowest number first.
    assert [i["number"] for i in picked] == [10]


def test_cap_full_dispatches_nothing():
    now = 1000 * HOUR
    state = {
        "1": {"status": "working", "dispatched_at": now},
        "2": {"status": "working", "dispatched_at": now},
    }
    picked = poller.select_issues(
        [issue(3)], state, set(), now=now, cap=2, stale_seconds=STALE
    )
    assert picked == []


def test_selection_is_oldest_first():
    now = 1000 * HOUR
    candidates = [issue(30), issue(3), issue(12)]
    picked = poller.select_issues(
        candidates, {}, set(), now=now, cap=2, stale_seconds=STALE
    )
    assert [i["number"] for i in picked] == [3, 12]


def test_state_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    issues = {"50": {"status": "working", "dispatched_at": 123.0, "branch": "agent/issue-50-x"}}
    poller.save_state(path, issues)
    assert poller.load_state(path) == issues
    # A missing/corrupt file loads as empty, never raises.
    assert poller.load_state(tmp_path / "nope.json") == {}
    (tmp_path / "bad.json").write_text("{not json")
    assert poller.load_state(tmp_path / "bad.json") == {}


def test_runtime_restart_path_classification():
    assert poller.needs_runtime_restart(["src/agentdeck/runtime.py"])
    assert poller.needs_runtime_restart(["uv.lock"])
    assert not poller.needs_runtime_restart(["src/agentdeck/web/routes_pages.py"])
    assert not poller.needs_runtime_restart(["kanban/README.md"])


_BAD_OBJECT_STDERR = (
    "fatal: bad object refs/remotes/origin/agent/issue-134-done\n"
    "error: https://github.com/x/y.git did not send all necessary objects\n"
    "error: refs/remotes/origin/agent/issue-134-done does not point to a valid object!\n"
)


def test_fetch_base_prunes_corrupt_tracking_ref_then_retries(monkeypatch):
    commands = []

    def fake_run(cmd, *, cwd=None, check=True):
        commands.append(cmd)
        # First fetch fails on the dangling ref; the retry (after prune) succeeds.
        if cmd[-2:] == ["origin", "master"]:
            fetched = sum(1 for c in commands if c[-2:] == ["origin", "master"])
            if fetched == 1:
                return subprocess.CompletedProcess(cmd, 1, "", _BAD_OBJECT_STDERR)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(poller, "_run", fake_run)
    poller._fetch_base(Path("/repo"), "master")

    deletes = [c for c in commands if c[-3:-1] == ["update-ref", "-d"]]
    assert deletes == [
        ["git", "-C", "/repo", "update-ref", "-d",
         "refs/remotes/origin/agent/issue-134-done"]
    ]
    assert sum(1 for c in commands if c[-2:] == ["origin", "master"]) == 2


def test_fetch_base_reraises_unrelated_fetch_failure(monkeypatch):
    def fake_run(cmd, *, cwd=None, check=True):
        return subprocess.CompletedProcess(cmd, 1, "", "fatal: could not read from remote")

    monkeypatch.setattr(poller, "_run", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        poller._fetch_base(Path("/repo"), "master")


def test_autodeploy_defers_runtime_until_chats_are_idle(tmp_path, monkeypatch):
    old = "a" * 40
    new = "b" * 40
    deploy_state = tmp_path / "deploy.json"
    poller.save_deploy_state(
        deploy_state, {"web_sha": old, "runtime_sha": old}
    )
    cfg = {
        "repo_path": str(tmp_path),
        "base_branch": "master",
        "autodeploy": {
            "enabled": True,
            "state_path": str(deploy_state),
            "web_service": "agentdeck.service",
            "runtime_service": "agentdeck-codex.service",
            "runtime_socket": "/tmp/runtime.sock",
            "health_url": "http://localhost/healthz",
        },
    }
    current = {"sha": old}
    commands = []
    restarts = []
    activity = {"active": True, "active_turns": {"codex": 1, "claude": 0}}

    def fake_run(cmd, *, cwd=None, check=True):
        commands.append((cmd, cwd, check))
        stdout = ""
        if cmd[-3:] == ["rev-parse", "--abbrev-ref", "HEAD"]:
            stdout = "master\n"
        if cmd[-2:] == ["status", "--porcelain"]:
            stdout = ""
        if "merge" in cmd and "--ff-only" in cmd:
            current["sha"] = new
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    monkeypatch.setattr(poller, "_run", fake_run)
    monkeypatch.setattr(
        poller,
        "_revision",
        lambda _repo, ref: new if ref == "origin/master" else current["sha"],
    )
    monkeypatch.setattr(
        poller, "_changed_paths", lambda _repo, _old, _new: ["src/agentdeck/runtime.py"]
    )
    monkeypatch.setattr(poller, "_runtime_activity", lambda _socket: activity)
    monkeypatch.setattr(poller, "_restart_web", lambda _cfg: restarts.append("web"))
    monkeypatch.setattr(poller, "_restart_runtime", lambda _cfg: restarts.append("runtime"))

    poller.autodeploy(cfg)

    assert poller.load_deploy_state(deploy_state) == {
        "web_sha": new,
        "runtime_sha": old,
    }
    assert restarts == ["web"]
    assert any(cmd[:2] == ["uv", "sync"] for cmd, _cwd, _check in commands)

    activity["active"] = False
    poller.autodeploy(cfg)

    assert poller.load_deploy_state(deploy_state) == {
        "web_sha": new,
        "runtime_sha": new,
    }
    assert restarts == ["web", "runtime"]
