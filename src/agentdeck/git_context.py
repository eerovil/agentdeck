"""Authoritative git and GitHub pull-request context for Deckhand."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass

from .models import Session

log = logging.getLogger(__name__)

_GITHUB_REMOTE_RE = re.compile(
    r"(?:https?://github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_PR_URL_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)",
    re.IGNORECASE,
)
_QUALIFIED_PR_RE = re.compile(
    r"\b(?:PR|pull request)\b[ \t:*_`-]*"
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)\b",
    re.IGNORECASE,
)
_GITHUB_REPO_URL_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:/|$)",
    re.IGNORECASE,
)
_PR_NUMBER_RE = re.compile(
    r"\b(?:PR|pull request)\s*(?:[*_`]+\s*)*#?(\d+)\b",
    re.IGNORECASE,
)

_PR_FIELDS = "number,title,url,state,isDraft,mergedAt,headRefName,baseRefName"
_DEFAULT_BRANCHES = {"main", "master"}


@dataclass(frozen=True)
class PullRequestContext:
    repository: str
    number: int
    title: str
    url: str
    status: str
    draft: bool = False
    head_branch: str | None = None
    base_branch: str | None = None

    def as_json(self) -> dict:
        return {
            "repository": self.repository,
            "number": self.number,
            "title": self.title,
            "url": self.url,
            "status": self.status,
            "draft": self.draft,
            "head_branch": self.head_branch,
            "base_branch": self.base_branch,
        }


@dataclass(frozen=True)
class GitContext:
    repository: str | None
    branch: str | None
    dirty: bool
    pull_requests: tuple[PullRequestContext, ...] = ()

    def as_json(self) -> dict:
        return {
            "repository": self.repository,
            "branch": self.branch,
            "dirty": self.dirty,
            "pull_requests": [pull.as_json() for pull in self.pull_requests],
        }


def github_repository(remote: str) -> str | None:
    """Return ``owner/repo`` for a github.com remote."""
    match = _GITHUB_REMOTE_RE.search(remote.strip())
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def _pull_request(repository: str, value: dict) -> PullRequestContext | None:
    number = value.get("number")
    title = value.get("title")
    url = value.get("url")
    state = value.get("state")
    if not isinstance(number, int) or not all(
        isinstance(item, str) for item in (title, url, state)
    ):
        return None
    status = "merged" if value.get("mergedAt") else state.lower()
    return PullRequestContext(
        repository=repository,
        number=number,
        title=title.strip(),
        url=url,
        status=status,
        draft=bool(value.get("isDraft")),
        head_branch=value.get("headRefName") if isinstance(value.get("headRefName"), str) else None,
        base_branch=value.get("baseRefName") if isinstance(value.get("baseRefName"), str) else None,
    )


class GitContextResolver:
    """Resolve session worktrees and related PRs without giving Deckhand tools.

    Git state is cheap and refreshed with every Deckhand analysis opportunity.
    GitHub calls are cached more aggressively, with terminal PRs treated as stable.
    """

    OPEN_TTL_S = 60.0
    TERMINAL_TTL_S = 3600.0
    NEGATIVE_TTL_S = 300.0
    COMMAND_TIMEOUT_S = 6.0
    MAX_EXPLICIT_REFS = 10

    def __init__(self) -> None:
        self._git = shutil.which("git")
        self._gh = shutil.which("gh")
        self._branch_cache: dict[tuple[str, str], tuple[float, tuple[PullRequestContext, ...]]] = {}
        self._ref_cache: dict[tuple[str, int], tuple[float, PullRequestContext | None]] = {}
        self._branch_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._ref_locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._command_limit = asyncio.Semaphore(6)

    async def _run(self, *args: str) -> tuple[int, str]:
        async def invoke(env: dict[str, str] | None = None) -> tuple[int, bytes]:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), self.COMMAND_TIMEOUT_S)
            return (process.returncode or 0, stdout)

        try:
            async with self._command_limit:
                code, stdout = await invoke()
                if (
                    code != 0
                    and args[0] == self._gh
                    and ("GH_TOKEN" in os.environ or "GITHUB_TOKEN" in os.environ)
                ):
                    # A stale service token overrides gh's working hosts.yml login.
                    # Fall back to that login only after the explicit environment
                    # credentials fail.
                    env = os.environ.copy()
                    env.pop("GH_TOKEN", None)
                    env.pop("GITHUB_TOKEN", None)
                    code, stdout = await invoke(env)
        except (OSError, TimeoutError) as exc:
            log.debug("Deckhand context command failed: %s", exc)
            return (1, "")
        return (code, stdout.decode("utf-8", "replace"))

    async def resolve(self, sessions: list[Session]) -> dict[str, GitContext]:
        rows = await asyncio.gather(*(self._resolve_session(session) for session in sessions))
        return {
            session.key: context
            for session, context in zip(sessions, rows, strict=True)
            if context is not None
        }

    async def _resolve_session(self, session: Session) -> GitContext | None:
        branch = None
        dirty = False
        referenced = self._repository_from_session(session)
        checkout = None
        git_found = False
        if self._git is not None and session.cwd is not None:
            cwd = str(session.cwd)
            (status_code, status_text), (remote_code, remote_text) = await asyncio.gather(
                self._run(self._git, "-C", cwd, "status", "--porcelain=v1", "--branch"),
                self._run(self._git, "-C", cwd, "remote", "get-url", "origin"),
            )
            if status_code == 0:
                git_found = True
                lines = status_text.splitlines()
                if lines and lines[0].startswith("## "):
                    head = lines[0][3:].split("...", 1)[0]
                    if head and not head.startswith("HEAD "):
                        branch = head
                dirty = any(line and not line.startswith("## ") for line in lines)
            if remote_code == 0:
                checkout = github_repository(remote_text)

        # The checkout's own remote is authoritative for branch discovery, but a
        # repo named in the session's issue/prompt is where a bare "PR #123" from
        # the transcript usually lives — e.g. a worker whose cwd is a superproject
        # checkout while the PR is in a nested app repo. Try both for bare numbers.
        repository = checkout or referenced
        pull_repositories = list(dict.fromkeys(r for r in (checkout, referenced) if r))

        pulls: dict[tuple[str, int], PullRequestContext] = {}
        if self._gh is not None:
            refs = self._explicit_refs(session, pull_repositories)
            resolved = await asyncio.gather(
                *(self._pull_for_ref(repo, number) for repo, number in refs)
            )
            explicit_pulls = tuple(pull for pull in resolved if pull is not None)
            for pull in explicit_pulls:
                pulls[(pull.repository.lower(), pull.number)] = pull

            # A shared checkout may have moved to another chat's branch. Once
            # this chat names its own PR, expose local branch/dirty state only
            # when that checkout still matches one of the PR head branches.
            if (
                explicit_pulls
                and branch
                and not any(pull.head_branch == branch for pull in explicit_pulls)
            ):
                branch = None
                dirty = False

            # A PR explicitly named by the chat is the work this session owns.
            # This matters for shared checkouts: their current branch may belong
            # to a newer session after the old PR was merged. Only fall back to
            # branch discovery when the transcript did not resolve to a PR.
            # Long-lived default branches may themselves be the head of a
            # promotion PR (for example master -> staging). Such a PR belongs
            # to the repository, not to every chat sharing its checkout.
            if not pulls and repository and branch and branch not in _DEFAULT_BRANCHES:
                for pull in await self._pulls_for_branch(repository, branch):
                    pulls[(pull.repository.lower(), pull.number)] = pull

        ordered = tuple(sorted(pulls.values(), key=lambda pull: pull.number, reverse=True))
        if not git_found and repository is None and not ordered:
            return None
        return GitContext(repository, branch, dirty, ordered)

    @staticmethod
    def _repository_from_session(session: Session) -> str | None:
        text = "\n".join(
            value
            for value in (
                session.issue_url,
                session.initial_prompt,
                session.title,
                session.last_prompt,
                session.last_text,
            )
            if value
        )
        match = _GITHUB_REPO_URL_RE.search(text)
        return f"{match.group(1)}/{match.group(2)}" if match else None

    @staticmethod
    def _explicit_refs(
        session: Session, repositories: str | list[str] | None
    ) -> list[tuple[str, int]]:
        if repositories is None:
            candidates: list[str] = []
        elif isinstance(repositories, str):
            candidates = [repositories]
        else:
            candidates = repositories
        text = "\n".join(
            value
            for value in (
                session.initial_prompt,
                session.title,
                session.last_prompt,
                session.last_text,
                session.issue_url,
            )
            if value
        )
        refs: list[tuple[str, int]] = []
        # Full URLs carry their own repo — always authoritative.
        refs.extend(
            (f"{m.group(1)}/{m.group(2)}", int(m.group(3))) for m in _PR_URL_RE.finditer(text)
        )
        # Kanban handoffs commonly use ``PR: owner/repo#123`` instead of a URL.
        # Preserve that repository rather than trying the number against the
        # worker's shared checkout or issue-tracker repository.
        refs.extend(
            (m.group(1), int(m.group(2))) for m in _QUALIFIED_PR_RE.finditer(text)
        )
        # A bare "PR #123" has no repo; try each candidate (checkout + referenced).
        bare_numbers = [int(m.group(1)) for m in _PR_NUMBER_RE.finditer(text)]
        for repository in candidates:
            refs.extend((repository, number) for number in bare_numbers)
        unique: dict[tuple[str, int], None] = {}
        for repo, number in refs:
            unique.setdefault((repo, number), None)
        return list(unique)[: GitContextResolver.MAX_EXPLICIT_REFS]

    async def _pulls_for_branch(
        self, repository: str, branch: str
    ) -> tuple[PullRequestContext, ...]:
        key = (repository.lower(), branch)
        lock = self._branch_locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self._branch_cache.get(key)
            if cached is not None:
                fetched_at, pulls = cached
                ttl = (
                    self.TERMINAL_TTL_S
                    if pulls and all(pull.status in {"closed", "merged"} for pull in pulls)
                    else self.OPEN_TTL_S
                )
                if now - fetched_at < ttl:
                    return pulls
            assert self._gh is not None
            code, output = await self._run(
                self._gh,
                "pr",
                "list",
                "--repo",
                repository,
                "--head",
                branch,
                "--state",
                "all",
                "--limit",
                "10",
                "--json",
                _PR_FIELDS,
            )
            pulls: tuple[PullRequestContext, ...] = ()
            if code == 0:
                try:
                    values = json.loads(output)
                    if isinstance(values, list):
                        pulls = tuple(
                            pull
                            for value in values
                            if isinstance(value, dict)
                            if (pull := _pull_request(repository, value)) is not None
                        )
                except ValueError:
                    pass
            self._branch_cache[key] = (now, pulls)
            return pulls

    async def _pull_for_ref(self, repository: str, number: int) -> PullRequestContext | None:
        key = (repository.lower(), number)
        lock = self._ref_locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self._ref_cache.get(key)
            if cached is not None:
                fetched_at, pull = cached
                ttl = (
                    self.NEGATIVE_TTL_S
                    if pull is None
                    else self.TERMINAL_TTL_S
                    if pull.status in {"closed", "merged"}
                    else self.OPEN_TTL_S
                )
                if now - fetched_at < ttl:
                    return pull
            assert self._gh is not None
            code, output = await self._run(
                self._gh,
                "pr",
                "view",
                str(number),
                "--repo",
                repository,
                "--json",
                _PR_FIELDS,
            )
            pull = None
            if code == 0:
                try:
                    value = json.loads(output)
                    if isinstance(value, dict):
                        pull = _pull_request(repository, value)
                except ValueError:
                    pass
            self._ref_cache[key] = (now, pull)
            return pull
