from __future__ import annotations

import json

import pytest

from agentdeck.git_context import GitContextResolver, github_repository
from agentdeck.models import Session, SessionStatus


def _session(tmp_path, *, initial_prompt=None, last_text=None, issue_url=None):
    return Session(
        key="codex:test:thread-1",
        account_key="codex:test",
        session_id="thread-1",
        status=SessionStatus.IDLE,
        cwd=tmp_path,
        title="Ship the feature",
        initial_prompt=initial_prompt,
        last_text=last_text,
        issue_url=issue_url,
        show_when_idle=True,
    )


def test_github_repository_parses_https_and_ssh():
    assert github_repository("https://github.com/eerovil/agentdeck.git") == "eerovil/agentdeck"
    assert github_repository("git@github.com:protecomp/storm.git") == "protecomp/storm"
    assert github_repository("https://example.com/eerovil/agentdeck.git") is None


def test_explicit_refs_recognize_markdown_formatted_pr_number(tmp_path):
    session = _session(
        tmp_path,
        initial_prompt="Work on protecomp/storm#253.",
        last_text="Deliverable: PR **#254** (`Closes #253`) is docs-only.",
    )

    assert GitContextResolver._explicit_refs(session, "protecomp/storm") == [
        ("protecomp/storm", 254),
    ]


def test_explicit_refs_try_bare_number_against_every_candidate_repo(tmp_path):
    # A worker whose checkout is a superproject (docker) but whose PR lives in a
    # nested app repo (tilhi, named via the issue URL) must resolve the bare
    # number against both candidates, not just the cwd remote.
    session = _session(
        tmp_path,
        issue_url="https://github.com/ScandinavianOutdoor/tilhi/issues/1634",
        last_text="Shipped a guardrail and tests in PR #1628, with orders tests green.",
    )

    refs = GitContextResolver._explicit_refs(
        session, ["ScandinavianOutdoor/docker", "ScandinavianOutdoor/tilhi"]
    )

    assert ("ScandinavianOutdoor/tilhi", 1628) in refs
    assert ("ScandinavianOutdoor/docker", 1628) in refs


def test_explicit_refs_full_url_wins_regardless_of_candidates(tmp_path):
    session = _session(
        tmp_path,
        last_text="Opened https://github.com/ScandinavianOutdoor/tilhi/pull/1628 for review.",
    )

    assert GitContextResolver._explicit_refs(session, []) == [
        ("ScandinavianOutdoor/tilhi", 1628),
    ]


def test_explicit_refs_qualified_handoff_wins_over_checkout_and_issue_repo(tmp_path):
    session = _session(
        tmp_path,
        issue_url="https://github.com/ScandinavianOutdoor/issues/issues/1635",
        last_text=(
            "Done.\n\n"
            "- **PR:** ScandinavianOutdoor/tilhi#1636 (ready — tests green).\n"
            "- **Board:** In review."
        ),
    )

    assert GitContextResolver._explicit_refs(
        session, ["ScandinavianOutdoor/docker", "ScandinavianOutdoor/issues"]
    ) == [("ScandinavianOutdoor/tilhi", 1636)]


async def test_resolves_merged_pr_for_worktree_branch(tmp_path, monkeypatch):
    resolver = GitContextResolver()
    resolver._git = "git"
    resolver._gh = "gh"
    calls = []

    async def fake_run(*args):
        calls.append(args)
        if "status" in args:
            return (0, "## feature/deckhand...origin/feature/deckhand\n M src/app.py\n")
        if "remote" in args:
            return (0, "git@github.com:eerovil/agentdeck.git\n")
        if args[1:3] == ("pr", "list"):
            return (
                0,
                json.dumps(
                    [
                        {
                            "number": 91,
                            "title": "Add PR context",
                            "url": "https://github.com/eerovil/agentdeck/pull/91",
                            "state": "MERGED",
                            "isDraft": False,
                            "mergedAt": "2026-07-15T07:00:00Z",
                            "headRefName": "feature/deckhand",
                            "baseRefName": "master",
                        }
                    ]
                ),
            )
        raise AssertionError(args)

    monkeypatch.setattr(resolver, "_run", fake_run)
    contexts = await resolver.resolve([_session(tmp_path)])

    context = contexts["codex:test:thread-1"]
    assert context.repository == "eerovil/agentdeck"
    assert context.branch == "feature/deckhand"
    assert context.dirty is True
    assert context.pull_requests[0].status == "merged"
    assert context.pull_requests[0].number == 91
    assert any(call[1:3] == ("pr", "list") for call in calls)


@pytest.mark.parametrize("branch", ["main", "master"])
async def test_default_branch_does_not_trigger_branch_pr_discovery(
    branch, tmp_path, monkeypatch
):
    resolver = GitContextResolver()
    resolver._git = "git"
    resolver._gh = "gh"
    branch_lookups = 0

    async def fake_run(*args):
        nonlocal branch_lookups
        if "status" in args:
            return (0, f"## {branch}...origin/{branch}\n")
        if "remote" in args:
            return (0, "git@github.com:protecomp/storm.git\n")
        if args[1:3] == ("pr", "list"):
            branch_lookups += 1
            raise AssertionError("default branches must not trigger PR discovery")
        raise AssertionError(args)

    monkeypatch.setattr(resolver, "_run", fake_run)
    context = (await resolver.resolve([_session(tmp_path)]))["codex:test:thread-1"]

    assert context.branch == branch
    assert context.pull_requests == ()
    assert branch_lookups == 0


async def test_resolves_explicit_pr_number_when_branch_has_no_pr(tmp_path, monkeypatch):
    resolver = GitContextResolver()
    resolver._git = "git"
    resolver._gh = "gh"

    async def fake_run(*args):
        if "status" in args:
            return (0, "## master...origin/master\n")
        if "remote" in args:
            return (0, "https://github.com/eerovil/agentdeck.git\n")
        if args[1:3] == ("pr", "list"):
            return (0, "[]")
        if args[1:3] == ("pr", "view"):
            assert args[3] == "92"
            return (
                0,
                json.dumps(
                    {
                        "number": 92,
                        "title": "Still in review",
                        "url": "https://github.com/eerovil/agentdeck/pull/92",
                        "state": "OPEN",
                        "isDraft": True,
                        "mergedAt": None,
                        "headRefName": "feature/review",
                        "baseRefName": "master",
                    }
                ),
            )
        raise AssertionError(args)

    monkeypatch.setattr(resolver, "_run", fake_run)
    contexts = await resolver.resolve([_session(tmp_path, last_text="Opened PR #92 for review.")])

    pull = contexts["codex:test:thread-1"].pull_requests[0]
    assert (pull.number, pull.status, pull.draft) == (92, "open", True)


async def test_bare_pr_candidate_miss_does_not_make_resolved_context_incomplete(
    tmp_path, monkeypatch
):
    resolver = GitContextResolver()
    resolver._git = "git"
    resolver._gh = "gh"

    async def fake_run(*args):
        if "status" in args:
            return (0, "## master...origin/master\n")
        if "remote" in args:
            return (0, "https://github.com/ScandinavianOutdoor/docker.git\n")
        if args[1:3] == ("pr", "view"):
            repository = args[5]
            if repository == "ScandinavianOutdoor/docker":
                return (1, "")
            assert repository == "ScandinavianOutdoor/tilhi"
            return (
                0,
                json.dumps(
                    {
                        "number": 1657,
                        "title": "Ready for review",
                        "url": "https://github.com/ScandinavianOutdoor/tilhi/pull/1657",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergedAt": None,
                        "headRefName": "fix/deckhand",
                        "baseRefName": "master",
                    }
                ),
            )
        raise AssertionError(args)

    monkeypatch.setattr(resolver, "_run", fake_run)
    session = _session(
        tmp_path,
        issue_url="https://github.com/ScandinavianOutdoor/tilhi/issues/151",
        last_text="Opened PR #1657 for review.",
    )

    context = (await resolver.resolve([session]))[session.key]

    assert [pull.number for pull in context.pull_requests] == [1657]
    assert context.pulls_complete is True


async def test_failed_qualified_ref_is_not_masked_by_same_number_bare_ref(
    tmp_path, monkeypatch
):
    resolver = GitContextResolver()
    resolver._git = "git"
    resolver._gh = "gh"

    async def fake_run(*args):
        if "status" in args:
            return (0, "## feature...origin/feature\n")
        if "remote" in args:
            return (0, "https://github.com/acme/checkout.git\n")
        if args[1:3] == ("pr", "view"):
            repository = args[5]
            if repository == "other/repo":
                return (1, "")
            assert repository == "acme/checkout"
            return (
                0,
                json.dumps(
                    {
                        "number": 151,
                        "title": "Checkout PR",
                        "url": "https://github.com/acme/checkout/pull/151",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergedAt": None,
                        "headRefName": "feature",
                        "baseRefName": "master",
                    }
                ),
            )
        raise AssertionError(args)

    monkeypatch.setattr(resolver, "_run", fake_run)
    session = _session(
        tmp_path,
        last_text=(
            "Opened PR #151 and also https://github.com/other/repo/pull/151."
        ),
    )

    context = (await resolver.resolve([session]))[session.key]

    assert [pull.repository for pull in context.pull_requests] == ["acme/checkout"]
    assert context.pulls_complete is False


async def test_explicit_merged_pr_outranks_unrelated_shared_checkout_branch(tmp_path, monkeypatch):
    resolver = GitContextResolver()
    resolver._git = "git"
    resolver._gh = "gh"
    branch_lookups = 0

    async def fake_run(*args):
        nonlocal branch_lookups
        if "status" in args:
            return (0, "## newer-work...origin/newer-work\n M search.py\n")
        if "remote" in args:
            return (0, "git@github.com:protecomp/storm.git\n")
        if args[1:3] == ("pr", "view"):
            return (
                0,
                json.dumps(
                    {
                        "number": 252,
                        "title": "Older completed work",
                        "url": "https://github.com/protecomp/storm/pull/252",
                        "state": "MERGED",
                        "isDraft": False,
                        "mergedAt": "2026-07-14T13:17:40Z",
                        "headRefName": "older-work",
                        "baseRefName": "master",
                    }
                ),
            )
        if args[1:3] == ("pr", "list"):
            branch_lookups += 1
            return (0, "[]")
        raise AssertionError(args)

    monkeypatch.setattr(resolver, "_run", fake_run)
    session = _session(
        tmp_path,
        last_text="Merged: https://github.com/protecomp/storm/pull/252",
    )
    context = (await resolver.resolve([session]))[session.key]

    assert [pull.number for pull in context.pull_requests] == [252]
    assert context.pull_requests[0].status == "merged"
    assert branch_lookups == 0


async def test_initial_prompt_pr_outranks_unrelated_shared_checkout_branch(tmp_path, monkeypatch):
    resolver = GitContextResolver()
    resolver._git = "git"
    resolver._gh = "gh"
    branch_lookups = 0

    async def fake_run(*args):
        nonlocal branch_lookups
        if "status" in args:
            return (0, "## codex/issue-253-search-updates...origin/current\n")
        if "remote" in args:
            return (0, "git@github.com:protecomp/storm.git\n")
        if args[1:3] == ("pr", "view"):
            assert args[3] == "239"
            return (
                0,
                json.dumps(
                    {
                        "number": 239,
                        "title": "Custobar automated-campaign coupon activation",
                        "url": "https://github.com/protecomp/storm/pull/239",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergedAt": None,
                        "headRefName": "issue-238-custobar-coupon",
                        "baseRefName": "master",
                    }
                ),
            )
        if args[1:3] == ("pr", "list"):
            branch_lookups += 1
            raise AssertionError("shared checkout branch must not be attributed")
        raise AssertionError(args)

    monkeypatch.setattr(resolver, "_run", fake_run)
    session = _session(
        tmp_path,
        initial_prompt="GitHub issue #238; PR #239 is open and in review.",
        last_text="Corrected the logging level. Tests: 21 passed.",
    )
    context = (await resolver.resolve([session]))[session.key]

    assert [pull.number for pull in context.pull_requests] == [239]
    assert context.branch is None
    assert context.dirty is False
    assert branch_lookups == 0


async def test_resolves_multiple_explicit_prs_for_one_chat(tmp_path, monkeypatch):
    resolver = GitContextResolver()
    resolver._git = "git"
    resolver._gh = "gh"

    async def fake_run(*args):
        if "status" in args:
            return (0, "## shared...origin/shared\n")
        if "remote" in args:
            return (0, "git@github.com:protecomp/storm.git\n")
        if args[1:3] == ("pr", "view"):
            number = int(args[3])
            return (
                0,
                json.dumps(
                    {
                        "number": number,
                        "title": f"PR {number}",
                        "url": f"https://github.com/protecomp/storm/pull/{number}",
                        "state": "MERGED" if number == 247 else "OPEN",
                        "isDraft": False,
                        "mergedAt": "2026-07-14T13:17:40Z" if number == 247 else None,
                        "headRefName": f"work-{number}",
                        "baseRefName": "master",
                    }
                ),
            )
        raise AssertionError(args)

    monkeypatch.setattr(resolver, "_run", fake_run)
    session = _session(
        tmp_path,
        last_text=(
            "First https://github.com/protecomp/storm/pull/247 then "
            "https://github.com/protecomp/storm/pull/250"
        ),
    )
    context = (await resolver.resolve([session]))[session.key]

    assert [(pull.number, pull.status) for pull in context.pull_requests] == [
        (250, "open"),
        (247, "merged"),
    ]


async def test_resolves_merged_pr_after_worktree_was_removed(tmp_path, monkeypatch):
    resolver = GitContextResolver()
    resolver._git = "git"
    resolver._gh = "gh"

    async def fake_run(*args):
        if args[0] == "git":
            return (128, "")
        if args[1:3] == ("pr", "view"):
            assert args[3] == "250"
            assert args[5] == "protecomp/storm"
            return (
                0,
                json.dumps(
                    {
                        "number": 250,
                        "title": "Prevent category filter panel popping",
                        "url": "https://github.com/protecomp/storm/pull/250",
                        "state": "MERGED",
                        "isDraft": False,
                        "mergedAt": "2026-07-14T13:17:40Z",
                        "headRefName": "claude/issue-246-tag-filter-last",
                        "baseRefName": "master",
                    }
                ),
            )
        raise AssertionError(args)

    monkeypatch.setattr(resolver, "_run", fake_run)
    session = _session(
        tmp_path / "removed-worktree",
        last_text="PR #250 contains the fix.",
        issue_url="https://github.com/protecomp/storm/issues/246",
    )
    contexts = await resolver.resolve([session])

    context = contexts[session.key]
    assert context.repository == "protecomp/storm"
    assert context.branch is None
    assert context.pull_requests[0].status == "merged"


async def test_gh_command_retries_without_failed_environment_token(monkeypatch):
    resolver = GitContextResolver()
    resolver._gh = "gh"
    monkeypatch.setenv("GH_TOKEN", "stale-token")
    calls = []

    class Process:
        returncode = 1

        async def communicate(self):
            return (b"", b"")

    async def create(*args, **kwargs):
        calls.append(kwargs.get("env"))
        process = Process()
        if kwargs.get("env") is not None:
            process.returncode = 0
            process.communicate = lambda: _communicate(b"[]")
        return process

    async def _communicate(output):
        return (output, b"")

    monkeypatch.setattr("agentdeck.git_context.asyncio.create_subprocess_exec", create)
    code, output = await resolver._run("gh", "pr", "list")

    assert (code, output) == (0, "[]")
    assert calls[0] is None
    assert "GH_TOKEN" not in calls[1]


async def test_explicit_pr_failure_retains_cached_context_and_retries(tmp_path, monkeypatch):
    resolver = GitContextResolver()
    resolver._git = "git"
    resolver._gh = "gh"
    resolver.OPEN_TTL_S = 0
    github_calls = 0

    async def fake_run(*args):
        nonlocal github_calls
        if "status" in args:
            return (0, "## master...origin/master\n")
        if "remote" in args:
            return (0, "https://github.com/eerovil/agentdeck.git\n")
        if args[1:3] == ("pr", "view"):
            github_calls += 1
            if github_calls == 2:
                return (1, "")  # e.g. GitHub API rate limiting
            return (
                0,
                json.dumps(
                    {
                        "number": 151,
                        "title": "Keep done sessions dismissed",
                        "url": "https://github.com/eerovil/agentdeck/pull/151",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergedAt": None,
                        "headRefName": "fix/deckhand",
                        "baseRefName": "master",
                    }
                ),
            )
        raise AssertionError(args)

    monkeypatch.setattr(resolver, "_run", fake_run)
    session = _session(tmp_path, last_text="Opened PR #151 for review.")

    first = (await resolver.resolve([session]))[session.key]
    unavailable = (await resolver.resolve([session]))[session.key]
    recovered = (await resolver.resolve([session]))[session.key]

    assert [pull.number for pull in first.pull_requests] == [151]
    assert unavailable.pull_requests == first.pull_requests
    assert unavailable.pulls_complete is False
    assert recovered.pull_requests == first.pull_requests
    assert recovered.pulls_complete is True
    assert github_calls == 3  # the failure was not negative-cached


def _pull(status: str, *, draft: bool = False):
    from agentdeck.git_context import PullRequestContext

    return PullRequestContext(
        repository="o/r",
        number=1,
        title="t",
        url="https://github.com/o/r/pull/1",
        status=status,
        draft=draft,
    )


@pytest.mark.parametrize(
    ("status", "is_open", "is_merged", "is_terminal"),
    [
        ("open", True, False, False),
        ("merged", False, True, True),
        ("closed", False, False, True),
        # An unexpected status is neither open nor terminal, so it is never
        # silently cached as done or suppressed as resolved.
        ("locked", False, False, False),
    ],
)
def test_pull_request_status_predicates(status, is_open, is_merged, is_terminal):
    pull = _pull(status)
    assert pull.is_open is is_open
    assert pull.is_merged is is_merged
    assert pull.is_terminal is is_terminal


def test_terminal_is_not_merely_the_complement_of_open():
    # closed and merged are both terminal but only one is merged; a draft is
    # still open (draft-ness is orthogonal to status).
    assert _pull("closed").is_terminal and not _pull("closed").is_merged
    assert _pull("open", draft=True).is_open


def test_pull_is_reviewable_and_display_status():
    assert _pull("open").is_reviewable is True
    assert _pull("open", draft=True).is_reviewable is False  # a draft is not reviewable
    assert _pull("merged").is_reviewable is False
    # display_status is the chip LABEL, not a boolean: an open draft reads "draft".
    assert _pull("open").display_status == "open"
    assert _pull("open", draft=True).display_status == "draft"
    assert _pull("merged").display_status == "merged"
    assert _pull("closed").display_status == "closed"


def test_reviewable_pull_returns_first_match_or_none():
    from agentdeck.git_context import GitContext

    ctx = GitContext("r", "b", False, (_pull("merged"), _pull("open"), _pull("open", draft=True)))
    assert ctx.reviewable_pull is ctx.pull_requests[1]  # first reviewable in order
    assert GitContext("r", "b", False, (_pull("merged"), _pull("closed"))).reviewable_pull is None
    assert GitContext("r", "b", False, ()).reviewable_pull is None


def test_is_shipped_requires_prs_and_all_terminal():
    from agentdeck.git_context import GitContext

    assert GitContext("r", "b", False, (_pull("merged"), _pull("closed"))).is_shipped is True
    assert GitContext("r", "b", False, (_pull("merged"), _pull("open"))).is_shipped is False
    assert GitContext("r", "b", False, ()).is_shipped is False  # empty is NOT shipped


def test_has_merged_pr_only_counts_actual_merges():
    from agentdeck.git_context import GitContext

    assert GitContext("r", "b", False, (_pull("merged"), _pull("open"))).has_merged_pr is True
    assert GitContext("r", "b", False, (_pull("closed"), _pull("open"))).has_merged_pr is False
    assert GitContext("r", "b", False, ()).has_merged_pr is False
