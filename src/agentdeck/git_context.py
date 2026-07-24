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
from pathlib import Path

from .github_cache import GitHubMetadata, GitHubMetadataCache
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
    status: str  # "open" | "merged" | "closed"; interpret via the properties below
    draft: bool = False
    head_branch: str | None = None
    base_branch: str | None = None

    @property
    def is_open(self) -> bool:
        """The PR is still open (a merged/closed PR is not)."""
        return self.status == "open"

    @property
    def is_merged(self) -> bool:
        """The PR actually merged (a PR closed without merging did not)."""
        return self.status == "merged"

    @property
    def is_terminal(self) -> bool:
        """The PR is resolved — merged or closed — so nothing is left to do.

        Kept an explicit set rather than ``not is_open`` so an unexpected status
        string is never treated as terminal by default.
        """
        return self.status in {"merged", "closed"}

    @property
    def is_reviewable(self) -> bool:
        """Open and not a draft — a human could review it now."""
        return self.is_open and not self.draft

    @property
    def display_status(self) -> str:
        """The chip label: an open draft reads ``draft``, otherwise the raw
        status. (The CSS class stays keyed on the raw ``status``.)"""
        return "draft" if self.draft and self.is_open else self.status

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
    pulls_complete: bool = True

    @property
    def reviewable_pull(self) -> PullRequestContext | None:
        """The first reviewable PR (highest-numbered, by resolver ordering), or None."""
        return next((pull for pull in self.pull_requests if pull.is_reviewable), None)

    @property
    def is_shipped(self) -> bool:
        """Has resolved PRs and every one is terminal. An empty set is NOT
        shipped — a session with no PRs stays eligible for a finished card."""
        if not self.pulls_complete or not self.pull_requests:
            return False
        return all(pull.is_terminal for pull in self.pull_requests)

    @property
    def has_merged_pr(self) -> bool:
        """At least one PR actually merged (closed-without-merging does not count)."""
        return any(pull.is_merged for pull in self.pull_requests)

    def as_json(self) -> dict:
        return {
            "repository": self.repository,
            "branch": self.branch,
            "dirty": self.dirty,
            "pull_requests": [pull.as_json() for pull in self.pull_requests],
            "pulls_complete": self.pulls_complete,
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

    COMMAND_TIMEOUT_S = 6.0
    MAX_EXPLICIT_REFS = 10

    def __init__(
        self,
        *,
        cache_path: Path | None = None,
        metadata_cache: GitHubMetadataCache | None = None,
    ) -> None:
        self._git = shutil.which("git")
        self._gh = shutil.which("gh")
        self._metadata = metadata_cache or GitHubMetadataCache(cache_path)
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

    async def resolve(
        self, sessions: list[Session], *, force: bool = False
    ) -> dict[str, GitContext]:
        fresh_after = time.time() if force else None
        rows = await asyncio.gather(
            *(
                self._resolve_session(
                    session, force=force, fresh_after=fresh_after
                )
                for session in sessions
            )
        )
        return {
            session.key: context
            for session, context in zip(sessions, rows, strict=True)
            if context is not None
        }

    async def _resolve_session(
        self,
        session: Session,
        *,
        force: bool = False,
        fresh_after: float | None = None,
    ) -> GitContext | None:
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
        ref_groups = self._explicit_ref_groups(session, pull_repositories)
        refs = list(dict.fromkeys(ref for group in ref_groups for ref in group))
        branch_lookup = bool(
            repository and branch and branch not in _DEFAULT_BRANCHES
        )

        pulls: dict[tuple[str, int], PullRequestContext] = {}
        pulls_complete = self._gh is not None or not (refs or branch_lookup)
        if self._gh is not None:
            resolved = await asyncio.gather(
                *(
                    self._pull_for_ref(
                        repo, number, force=force, fresh_after=fresh_after
                    )
                    for repo, number in refs
                )
            )
            by_ref = dict(zip(refs, resolved, strict=True))
            explicit_pulls = tuple(
                pull for pull, _complete in resolved if pull is not None
            )
            # URL/qualified references are singleton groups and must resolve.
            # A bare ``PR #123`` is an alternative group: one successful candidate
            # repository makes expected misses in the others harmless.
            pulls_complete = all(
                all(by_ref[ref][1] for ref in group)
                or any(
                    pull is not None and complete
                    for pull, complete in (by_ref[ref] for ref in group)
                )
                for group in ref_groups
            )
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
            if not pulls and branch_lookup:
                assert repository is not None and branch is not None
                branch_pulls, branch_complete = await self._pulls_for_branch(
                    repository,
                    branch,
                    force=force,
                    fresh_after=fresh_after,
                )
                pulls_complete = pulls_complete and branch_complete
                for pull in branch_pulls:
                    pulls[(pull.repository.lower(), pull.number)] = pull

        ordered = tuple(sorted(pulls.values(), key=lambda pull: pull.number, reverse=True))
        if not git_found and repository is None and not ordered:
            return None
        return GitContext(repository, branch, dirty, ordered, pulls_complete)

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
    def _reference_text(session: Session) -> str:
        return "\n".join(
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

    @classmethod
    def _bare_pr_numbers(cls, session: Session) -> list[int]:
        return list(
            dict.fromkeys(
                int(match.group(1))
                for match in _PR_NUMBER_RE.finditer(cls._reference_text(session))
            )
        )

    @classmethod
    def _explicit_ref_groups(
        cls, session: Session, repositories: str | list[str] | None
    ) -> list[tuple[tuple[str, int], ...]]:
        if repositories is None:
            candidates: list[str] = []
        elif isinstance(repositories, str):
            candidates = [repositories]
        else:
            candidates = repositories
        text = cls._reference_text(session)
        groups: list[tuple[tuple[str, int], ...]] = []
        # Full URLs carry their own repo — always authoritative.
        groups.extend(
            ((f"{m.group(1)}/{m.group(2)}", int(m.group(3))),)
            for m in _PR_URL_RE.finditer(text)
        )
        # Kanban handoffs commonly use ``PR: owner/repo#123`` instead of a URL.
        # Preserve that repository rather than trying the number against the
        # worker's shared checkout or issue-tracker repository.
        groups.extend(
            ((m.group(1), int(m.group(2))),)
            for m in _QUALIFIED_PR_RE.finditer(text)
        )
        # A bare "PR #123" has no repo; try each candidate (checkout + referenced).
        bare_numbers = cls._bare_pr_numbers(session)
        groups.extend(
            tuple((repository, number) for repository in candidates)
            for number in bare_numbers
            if candidates
        )

        # Bound unique API calls while preserving shared refs in every semantic
        # group (for example the same PR named both by URL and bare number).
        selected: set[tuple[str, int]] = set()
        bounded: list[tuple[tuple[str, int], ...]] = []
        for group in groups:
            kept: list[tuple[str, int]] = []
            for ref in dict.fromkeys(group):
                if ref in selected or len(selected) < cls.MAX_EXPLICIT_REFS:
                    selected.add(ref)
                    kept.append(ref)
            if kept:
                bounded.append(tuple(kept))
        return bounded

    @classmethod
    def _explicit_refs(
        cls, session: Session, repositories: str | list[str] | None
    ) -> list[tuple[str, int]]:
        groups = cls._explicit_ref_groups(session, repositories)
        return list(dict.fromkeys(ref for group in groups for ref in group))

    async def _pulls_for_branch(
        self,
        repository: str,
        branch: str,
        *,
        force: bool = False,
        fresh_after: float | None = None,
    ) -> tuple[tuple[PullRequestContext, ...], bool]:
        async def fetch() -> GitHubMetadata | None:
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
            try:
                values = json.loads(output) if code == 0 else None
                if not isinstance(values, list):
                    raise ValueError("GitHub PR list was unavailable")
                parsed = tuple(
                    _pull_request(repository, value)
                    for value in values
                    if isinstance(value, dict)
                )
                if len(parsed) != len(values) or any(pull is None for pull in parsed):
                    raise ValueError("GitHub PR list was incomplete")
                pulls = tuple(pull for pull in parsed if pull is not None)
            except (TypeError, ValueError):
                log.debug(
                    "Deckhand PR lookup unavailable for %s branch %s",
                    repository,
                    branch,
                )
                return None
            return GitHubMetadata(
                values,
                terminal=bool(pulls) and all(pull.is_terminal for pull in pulls),
            )

        lookup = await self._metadata.resolve(
            f"pr-branch:v1:{repository.lower()}:{branch}",
            fetch,
            force=force,
            fresh_after=fresh_after,
        )
        try:
            values = lookup.value
            if not isinstance(values, list):
                raise ValueError("GitHub PR list cache was incomplete")
            parsed = tuple(
                _pull_request(repository, value)
                for value in values
                if isinstance(value, dict)
            )
            if len(parsed) != len(values) or any(pull is None for pull in parsed):
                raise ValueError("GitHub PR list cache was incomplete")
        except (TypeError, ValueError):
            return ((), False)
        return (tuple(pull for pull in parsed if pull is not None), lookup.complete)

    async def _pull_for_ref(
        self,
        repository: str,
        number: int,
        *,
        force: bool = False,
        fresh_after: float | None = None,
    ) -> tuple[PullRequestContext | None, bool]:
        async def fetch() -> GitHubMetadata | None:
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
            try:
                value = json.loads(output) if code == 0 else None
                pull = _pull_request(repository, value) if isinstance(value, dict) else None
                if pull is None:
                    raise ValueError("GitHub PR was unavailable")
            except (TypeError, ValueError):
                log.debug(
                    "Deckhand PR lookup unavailable for %s#%s",
                    repository,
                    number,
                )
                return None
            return GitHubMetadata(value, terminal=pull.is_terminal)

        lookup = await self._metadata.resolve(
            f"pr-ref:v1:{repository.lower()}:{number}",
            fetch,
            force=force,
            fresh_after=fresh_after,
        )
        value = lookup.value
        pull = _pull_request(repository, value) if isinstance(value, dict) else None
        return (pull, lookup.complete and pull is not None)
