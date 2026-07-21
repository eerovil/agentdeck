"""Unit tests for the kanban poller's deterministic core (kanban/poller.py).

These cover the selection/idempotency logic — the part that decides what gets
dispatched — with no gh/git/uv/delegate I/O. The side-effecting wrappers
(create_worktree, dispatch) are exercised live during rollout, not here.
"""

import importlib.util
from pathlib import Path

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
