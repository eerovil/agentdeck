import httpx
import pytest
import respx

from agentdeck.models import Account
from agentdeck.providers.claude_code import usage
from agentdeck.providers.claude_code.usage import (
    USAGE_URL,
    UsageAuthError,
    UsagePoller,
    UsageRateLimited,
    UsageUnavailable,
    fetch_usage_once,
)

OK_BODY = {
    "five_hour": {"utilization": 12.0, "resets_at": "2026-07-03T13:00:00Z"},
    "seven_day": {"utilization": 5.0, "resets_at": "2026-07-06T20:59:59Z"},
}


def _account(cfg_path) -> Account:
    return Account(key="claude_code:main", provider_id="claude_code", label="main", root=cfg_path)


@pytest.fixture
def account(tmp_path, write_credentials):
    write_credentials(tmp_path, "tok-1")
    return _account(tmp_path)


async def test_fetch_usage_once_happy(account):
    with respx.mock:
        respx.get(USAGE_URL).mock(return_value=httpx.Response(200, json=OK_BODY))
        async with httpx.AsyncClient() as client:
            snap = await fetch_usage_once(account, client)
    assert snap.five_hour_pct == 12.0
    assert snap.seven_day_pct == 5.0
    assert snap.five_hour_resets_at is not None
    assert snap.seven_day_resets_at.year == 2026


async def test_fetch_usage_once_401(account):
    with respx.mock:
        respx.get(USAGE_URL).mock(return_value=httpx.Response(401))
        async with httpx.AsyncClient() as client:
            with pytest.raises(UsageAuthError):
                await fetch_usage_once(account, client)


async def test_fetch_usage_once_429(account):
    with respx.mock:
        respx.get(USAGE_URL).mock(return_value=httpx.Response(429))
        async with httpx.AsyncClient() as client:
            with pytest.raises(UsageRateLimited):
                await fetch_usage_once(account, client)


async def test_fetch_usage_once_5xx_is_rate_limited(account):
    with respx.mock:
        respx.get(USAGE_URL).mock(return_value=httpx.Response(503))
        async with httpx.AsyncClient() as client:
            with pytest.raises(UsageRateLimited):
                await fetch_usage_once(account, client)


async def test_fetch_usage_once_malformed(account):
    with respx.mock:
        respx.get(USAGE_URL).mock(return_value=httpx.Response(200, text="not json"))
        async with httpx.AsyncClient() as client:
            with pytest.raises(UsageUnavailable):
                await fetch_usage_once(account, client)


async def test_fetch_usage_once_no_token(tmp_path):
    acc = _account(tmp_path)  # no .credentials.json written
    with respx.mock:
        route = respx.get(USAGE_URL).mock(return_value=httpx.Response(200, json=OK_BODY))
        async with httpx.AsyncClient() as client:
            with pytest.raises(UsageUnavailable):
                await fetch_usage_once(acc, client)
    assert not route.called  # short-circuits before any HTTP


async def test_token_read_fresh_each_call(tmp_path, write_credentials):
    """The bearer must be re-read from disk every request (tokens rotate)."""
    write_credentials(tmp_path, "tok-A")
    acc = _account(tmp_path)
    seen = []
    with respx.mock:

        def _capture(request):
            seen.append(request.headers.get("authorization"))
            return httpx.Response(200, json=OK_BODY)

        respx.get(USAGE_URL).mock(side_effect=_capture)
        async with httpx.AsyncClient() as client:
            await fetch_usage_once(acc, client)
            write_credentials(tmp_path, "tok-B")  # rotate on disk
            await fetch_usage_once(acc, client)
    assert seen == ["Bearer tok-A", "Bearer tok-B"]


async def test_poller_backoff_schedule(account):
    """On persistent 429 the delay doubles from the base interval."""
    delays: list[float] = []

    class Stop(Exception):
        pass

    async def fake_sleep(d: float) -> None:
        delays.append(d)
        if len(delays) > 4:
            raise Stop

    poller = UsagePoller(
        account,
        _FakeState(),
        interval_s=100.0,
        sleep=fake_sleep,
        jitter=lambda: 0.9,  # phase offset → 0
    )
    with respx.mock:
        respx.get(USAGE_URL).mock(return_value=httpx.Response(429))
        with pytest.raises(Stop):
            await poller.run()
    # delays[0] = phase offset (0); then exponential backoff
    assert delays[0] == pytest.approx(0.0)
    assert delays[1] == pytest.approx(100.0)
    assert delays[2] == pytest.approx(200.0)
    assert delays[3] == pytest.approx(400.0)


async def test_poller_publishes_on_success(account):
    state = _FakeState()

    class Stop(Exception):
        pass

    async def fake_sleep(d: float) -> None:
        if state.snapshots:
            raise Stop

    poller = UsagePoller(account, state, interval_s=100.0, sleep=fake_sleep, jitter=lambda: 0.9)
    with respx.mock:
        respx.get(USAGE_URL).mock(return_value=httpx.Response(200, json=OK_BODY))
        with pytest.raises(Stop):
            await poller.run()
    assert state.snapshots[-1].five_hour_pct == 12.0


class _FakeState:
    def __init__(self):
        self.snapshots = []
        self.stale = []

    def set_usage(self, snap):
        self.snapshots.append(snap)

    def mark_usage_stale(self, key):
        self.stale.append(key)


async def test_write_shared_cache(account, tmp_path):
    snap = usage.parse_usage(account.key, OK_BODY)
    cache = tmp_path / "cache"
    usage.write_shared_cache(snap, account, cache)
    written = (cache / "usage-main.json").read_text()
    assert '"five_hour_pct": 12.0' in written
