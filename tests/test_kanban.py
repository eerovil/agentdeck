from agentdeck.providers.claude_code import kanban


def test_parse_ref_basic():
    ref = kanban.parse_ref("Run the kanban-worker skill for ScandinavianOutdoor/store#2728.")
    assert ref is not None
    assert (ref.owner, ref.repo, ref.number, ref.mode) == (
        "ScandinavianOutdoor",
        "store",
        2728,
        None,
    )
    assert ref.key == "ScandinavianOutdoor/store#2728"
    assert ref.short == "store#2728"


def test_parse_ref_storm_variant():
    ref = kanban.parse_ref("Run the kanban-worker-storm skill for protecomp/storm#244.")
    assert ref is not None
    assert (ref.owner, ref.repo, ref.number) == ("protecomp", "storm", 244)


def test_parse_ref_captures_mode():
    ref = kanban.parse_ref(
        "Run the kanban-worker skill for ScandinavianOutdoor/issues#56 in REVIEW mode: "
        "run the review-fix-pr skill ..."
    )
    assert ref is not None and ref.number == 56 and ref.mode == "review"

    ref = kanban.parse_ref(
        "Run the kanban-worker skill for ScandinavianOutdoor/store#2716 in MERGE-FIX mode: ..."
    )
    assert ref is not None and ref.mode == "merge-fix"

    ref = kanban.parse_ref(
        "Run the kanban-worker skill for ScandinavianOutdoor/issues#53, "
        "RESUMING from the existing worktree ..."
    )
    assert ref is not None and ref.mode == "resume"


def test_parse_ref_ignores_non_kanban():
    assert kanban.parse_ref("Look at agentdeck project") is None
    assert kanban.parse_ref("") is None
    assert kanban.parse_ref(None) is None


def test_issue_url():
    ref = kanban.parse_ref("Run the kanban-worker skill for ScandinavianOutdoor/store#2728.")
    assert kanban.issue_url(ref) == "https://github.com/ScandinavianOutdoor/store/issues/2728"
    storm = kanban.parse_ref("Run the kanban-worker-storm skill for protecomp/storm#244.")
    assert kanban.issue_url(storm) == "https://github.com/protecomp/storm/issues/244"


def test_status_label():
    assert kanban.status_label(None) is None
    assert kanban.status_label({}) is None
    assert kanban.status_label({"state": "open", "is_pr": False}) == ("open", "open")
    assert kanban.status_label({"state": "open", "is_pr": True}) == ("open", "open")
    # PRs: merged vs closed-unmerged.
    assert kanban.status_label({"state": "closed", "is_pr": True, "merged": True}) == (
        "merged", "merged")
    assert kanban.status_label({"state": "closed", "is_pr": True, "merged": False}) == (
        "closed", "closed")
    # Issues: completed (done) vs not-planned (dropped).
    assert kanban.status_label(
        {"state": "closed", "is_pr": False, "state_reason": "completed"}) == ("closed", "done")
    assert kanban.status_label(
        {"state": "closed", "is_pr": False, "state_reason": "not_planned"}) == ("closed", "dropped")


def test_format_title():
    ref = kanban.parse_ref("Run the kanban-worker skill for ScandinavianOutdoor/store#2728.")
    assert kanban.format_title(ref, "Fix intro text duplication") == (
        "store#2728 · Fix intro text duplication"
    )
    # Unresolved title falls back to the bare reference.
    assert kanban.format_title(ref, None) == "store#2728"


def test_format_title_with_mode():
    ref = kanban.parse_ref(
        "Run the kanban-worker skill for ScandinavianOutdoor/issues#56 in REVIEW mode: ..."
    )
    assert kanban.format_title(ref, "Improve chatbot") == "issues#56 · Improve chatbot (review)"


def test_cache_persists_and_dedupes(tmp_path, monkeypatch):
    path = tmp_path / "kanban_titles.json"
    cache = kanban.KanbanTitleCache(path=path)
    cache._gh = "/usr/bin/gh"  # pretend gh exists

    calls: list[str] = []

    async def fake_fetch(ref):
        calls.append(ref.key)
        return (ref, {"title": f"Title for {ref.short}", "state": "open", "is_pr": False})

    monkeypatch.setattr(cache, "_fetch", fake_fetch)

    ref = kanban.parse_ref("Run the kanban-worker skill for ScandinavianOutdoor/store#2728.")
    dup_mode = kanban.parse_ref(
        "Run the kanban-worker skill for ScandinavianOutdoor/store#2728 in REVIEW mode: ..."
    )

    import asyncio

    # Two refs to the same issue → one fetch; title + state land in the cache/disk.
    changed = asyncio.run(cache.resolve_missing([ref, dup_mode], now=1000.0))
    assert changed is True
    assert calls == ["ScandinavianOutdoor/store#2728"]
    assert cache.get(ref) == "Title for store#2728"
    assert cache.get_status(ref) == ("open", "open")

    # A fresh instance reads the persisted record without re-fetching.
    reloaded = kanban.KanbanTitleCache(path=path)
    reloaded._gh = "/usr/bin/gh"
    assert reloaded.get(ref) == "Title for store#2728"
    assert reloaded.get_status(ref) == ("open", "open")
    # Open state re-polls after OPEN_TTL_S, not before.
    assert asyncio.run(reloaded.resolve_missing([ref], now=1000.0 + 60)) is False


def test_negative_cache_retries_after_ttl(tmp_path, monkeypatch):
    cache = kanban.KanbanTitleCache(path=tmp_path / "k.json")
    cache._gh = "/usr/bin/gh"

    result = {"rec": None}

    async def fake_fetch(ref):
        return (ref, result["rec"])

    monkeypatch.setattr(cache, "_fetch", fake_fetch)
    ref = kanban.parse_ref("Run the kanban-worker skill for ScandinavianOutdoor/issues#1.")

    import asyncio

    # Miss is cached; not retried before NEG_TTL_S.
    assert asyncio.run(cache.resolve_missing([ref], now=0.0)) is True
    assert cache.get(ref) is None
    assert asyncio.run(cache.resolve_missing([ref], now=cache.NEG_TTL_S - 1)) is False

    # After the negative TTL it retries, and now succeeds.
    result["rec"] = {"title": "Recovered title", "state": "closed", "state_reason": "completed"}
    assert asyncio.run(cache.resolve_missing([ref], now=cache.NEG_TTL_S + 1)) is True
    assert cache.get(ref) == "Recovered title"
    assert cache.get_status(ref) == ("closed", "done")
    # Terminal state uses the long TTL: past OPEN_TTL_S but under TERMINAL_TTL_S
    # it must NOT re-poll (an open issue would have).
    later = cache.NEG_TTL_S + 1 + cache.OPEN_TTL_S + 60
    assert cache.OPEN_TTL_S < (later - (cache.NEG_TTL_S + 1)) < cache.TERMINAL_TTL_S
    assert asyncio.run(cache.resolve_missing([ref], now=later)) is False


def test_resolve_noop_without_gh(tmp_path):
    cache = kanban.KanbanTitleCache(path=tmp_path / "k.json")
    cache._gh = None
    ref = kanban.parse_ref("Run the kanban-worker skill for ScandinavianOutdoor/store#9.")

    import asyncio

    assert asyncio.run(cache.resolve_missing([ref], now=0.0)) is False
    assert cache.get(ref) is None
