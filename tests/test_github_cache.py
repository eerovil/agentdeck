from __future__ import annotations

import asyncio

from agentdeck.github_cache import GitHubMetadata, GitHubMetadataCache


async def test_open_value_is_shared_for_five_minutes_and_force_bypasses_ttl(tmp_path):
    path = tmp_path / "github.sqlite3"
    first = GitHubMetadataCache(path)
    second = GitHubMetadataCache(path)
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return GitHubMetadata({"state": "open", "title": f"value {calls}"})

    initial = await first.resolve("issue:v1:o/r#1", fetch, now=1000.0)
    shared = await second.resolve("issue:v1:o/r#1", fetch, now=1299.0)
    forced = await second.resolve("issue:v1:o/r#1", fetch, now=1300.0, force=True)

    assert initial.value == {"state": "open", "title": "value 1"}
    assert shared.value == initial.value
    assert shared.refreshed is False
    assert forced.value == {"state": "open", "title": "value 2"}
    assert calls == 2


async def test_manual_refresh_boundary_deduplicates_same_key(tmp_path):
    cache = GitHubMetadataCache(tmp_path / "github.sqlite3")
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return GitHubMetadata({"title": f"value {calls}"})

    first = await cache.resolve(
        "pr-ref:v1:o/r:1",
        fetch,
        force=True,
        fresh_after=2000.0,
        now=2001.0,
    )
    reused = await cache.resolve(
        "pr-ref:v1:o/r:1",
        fetch,
        force=True,
        fresh_after=2000.0,
        now=2002.0,
    )

    assert first.value == reused.value
    assert calls == 1


async def test_cross_instance_lease_prevents_duplicate_refresh(tmp_path):
    path = tmp_path / "github.sqlite3"
    first = GitHubMetadataCache(path)
    second = GitHubMetadataCache(path)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_fetch():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return GitHubMetadata({"title": "one fetch"})

    first_task = asyncio.create_task(
        first.resolve("pr-ref:v1:o/r:2", slow_fetch, now=2000.0)
    )
    await started.wait()
    competing = await second.resolve("pr-ref:v1:o/r:2", slow_fetch, now=2000.0)
    release.set()
    resolved = await first_task

    assert calls == 1
    assert competing.value is None
    assert competing.complete is False
    assert resolved.value == {"title": "one fetch"}


async def test_expired_lease_owner_cannot_overwrite_newer_result(tmp_path):
    path = tmp_path / "github.sqlite3"
    slow = GitHubMetadataCache(path)
    takeover = GitHubMetadataCache(path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_fetch():
        started.set()
        await release.wait()
        return GitHubMetadata({"title": "late"})

    async def current_fetch():
        return GitHubMetadata({"title": "current"})

    slow_task = asyncio.create_task(
        slow.resolve("pr-ref:v1:o/r:5", slow_fetch, now=6000.0)
    )
    await started.wait()
    current = await takeover.resolve(
        "pr-ref:v1:o/r:5", current_fetch, now=6000.0 + slow.LEASE_S + 1
    )
    release.set()
    late = await slow_task

    assert current.value == {"title": "current"}
    assert late.value == current.value
    assert slow.peek("pr-ref:v1:o/r:5").value == current.value


async def test_transient_failure_preserves_trusted_stale_value(tmp_path):
    cache = GitHubMetadataCache(tmp_path / "github.sqlite3")

    async def succeeds():
        return GitHubMetadata({"state": "open", "title": "trusted"})

    async def fails():
        return None

    await cache.resolve("issue:v1:o/r#3", succeeds, now=3000.0)
    stale = await cache.resolve("issue:v1:o/r#3", fails, now=3301.0)
    backed_off = await cache.resolve("issue:v1:o/r#3", fails, now=3302.0)

    assert stale.value == {"state": "open", "title": "trusted"}
    assert stale.complete is False
    assert backed_off.value == stale.value
    assert backed_off.complete is False


async def test_terminal_value_uses_long_ttl(tmp_path):
    cache = GitHubMetadataCache(tmp_path / "github.sqlite3")
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return GitHubMetadata({"state": "closed"}, terminal=True)

    await cache.resolve("issue:v1:o/r#4", fetch, now=4000.0)
    result = await cache.resolve("issue:v1:o/r#4", fetch, now=4500.0)

    assert result.value == {"state": "closed"}
    assert calls == 1


async def test_batch_bounds_actual_refreshes_but_reuses_fresh_entries(tmp_path):
    cache = GitHubMetadataCache(tmp_path / "github.sqlite3")
    calls = []

    def request(key):
        async def fetch():
            calls.append(key)
            return GitHubMetadata({"key": key})

        return (key, fetch)

    requests = [request(f"issue:v1:o/r#{number}") for number in range(4)]
    first = await cache.resolve_many(requests, now=5000.0, max_refreshes=2)
    second = await cache.resolve_many(requests, now=5001.0, max_refreshes=2)

    assert len([result for result in first.values() if result.refreshed]) == 2
    assert len([result for result in second.values() if result.refreshed]) == 2
    assert calls == [
        "issue:v1:o/r#0",
        "issue:v1:o/r#1",
        "issue:v1:o/r#2",
        "issue:v1:o/r#3",
    ]
