from __future__ import annotations

import json

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
