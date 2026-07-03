import asyncio

from agentdeck.collector import Collector
from agentdeck.config import AccountConfig, AppConfig, PollingConfig
from agentdeck.state import AppState


async def test_collector_populates_sessions(fake_config_dir):
    config = AppConfig(
        polling=PollingConfig(scan_interval_s=0.05, liveness_interval_s=0.05, usage_interval_s=999),
        accounts=[
            AccountConfig(
                provider="claude_code", label="main", config_dir=str(fake_config_dir.path)
            )
        ],
    )
    state = AppState()
    collector = Collector(config, state)
    await collector.start()
    try:
        for _ in range(60):
            if state.sessions:
                break
            await asyncio.sleep(0.02)
    finally:
        await collector.stop()

    sids = {s.session_id for s in state.sessions.values()}
    assert fake_config_dir.live_sid in sids
    assert fake_config_dir.idle_sid in sids
