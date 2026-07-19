"""Agent-triggered runtime restart + autonomous continuation (issue #20)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentdeck.config import AppConfig
from agentdeck.providers.claude_code import restart
from agentdeck.providers.claude_code.restart import (
    RestartMarker,
    _unit_from_cgroup,
    detect_runtime_unit,
    looks_like_runtime_unit,
    read_markers,
    write_marker,
)
from agentdeck.providers.claude_code.worker import DeliverResult
from agentdeck.runtime import create_runtime_app


def _marker(session_id="sid-1", prompt="run the tests", created_at=None):
    return RestartMarker(
        session_id=session_id,
        prompt=prompt,
        service="agentdeck-staging-codex.service",
        created_at=time.time() if created_at is None else created_at,
    )


# --- marker persistence ----------------------------------------------------


def test_marker_round_trip(tmp_path):
    write_marker(tmp_path, _marker())
    entries = read_markers(tmp_path)
    assert len(entries) == 1
    _, loaded = entries[0]
    assert loaded.session_id == "sid-1"
    assert loaded.prompt == "run the tests"
    assert loaded.service == "agentdeck-staging-codex.service"


def test_read_markers_drops_malformed(tmp_path):
    write_marker(tmp_path, _marker())
    bad = restart.markers_dir(tmp_path) / "garbage.json"
    bad.write_text("{ not json")
    entries = read_markers(tmp_path)
    assert len(entries) == 1  # only the good one
    assert not bad.exists()  # the malformed file was cleaned up


def test_read_markers_missing_dir_is_empty(tmp_path):
    assert read_markers(tmp_path / "nope") == []


def test_odd_session_id_is_filesystem_safe(tmp_path):
    write_marker(tmp_path, _marker(session_id="owner/repo#12"))
    entries = read_markers(tmp_path)
    assert len(entries) == 1
    assert entries[0][1].session_id == "owner/repo#12"


# --- unit detection --------------------------------------------------------


def test_unit_from_cgroup_v2_leaf():
    line = (
        "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
        "agentdeck-staging-codex.service"
    )
    assert _unit_from_cgroup(line) == "agentdeck-staging-codex.service"


def test_unit_from_cgroup_none_when_absent():
    assert _unit_from_cgroup("0::/user.slice/session.scope") is None


def test_detect_runtime_unit_handles_missing_proc(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("no /proc here")

    monkeypatch.setattr(restart.Path, "read_text", boom)
    assert detect_runtime_unit() is None


@pytest.mark.parametrize(
    "service,expected",
    [
        ("agentdeck-codex.service", True),
        ("agentdeck-staging-codex.service", True),
        ("agentdeck-staging.service", False),  # web unit, not the runtime
        ("some-other.service", False),
    ],
)
def test_looks_like_runtime_unit(service, expected):
    assert looks_like_runtime_unit(service) is expected


# --- resume on boot --------------------------------------------------------


def _runtime(tmp_path):
    cfg = AppConfig.model_validate(
        {
            "claude_workers": {"enabled": True, "state_dir": str(tmp_path)},
            "accounts": [
                {"provider": "claude_code", "label": "main", "config_dir": "~/.claude"}
            ],
        }
    )
    return create_runtime_app(cfg).state.claude_workers


async def test_resume_delivers_to_matching_session_and_clears_marker(tmp_path):
    workers = _runtime(tmp_path)
    host = MagicMock()
    host.find_key_by_session = MagicMock(return_value="issue-1")
    host.deliver = AsyncMock(return_value=DeliverResult(True, "revived", session_id="sid-1"))
    workers.hosts["main"] = host

    write_marker(tmp_path, _marker(session_id="sid-1", prompt="run pytest"))
    await workers.resume_after_restart()

    host.deliver.assert_awaited_once()
    args, kwargs = host.deliver.await_args
    assert args[0] == "issue-1" and args[1] == "run pytest"
    assert kwargs["delivery_id"].startswith("restart-continue:sid-1:")
    assert read_markers(tmp_path) == []  # marker consumed


async def test_resume_skips_stale_marker(tmp_path):
    workers = _runtime(tmp_path)
    host = MagicMock()
    host.find_key_by_session = MagicMock(return_value="issue-1")
    host.deliver = AsyncMock()
    workers.hosts["main"] = host

    write_marker(
        tmp_path,
        _marker(session_id="sid-1", created_at=time.time() - restart.MARKER_TTL_S - 60),
    )
    await workers.resume_after_restart()

    host.deliver.assert_not_awaited()
    assert read_markers(tmp_path) == []  # stale marker still dropped


async def test_resume_drops_marker_when_session_unknown(tmp_path):
    workers = _runtime(tmp_path)
    host = MagicMock()
    host.find_key_by_session = MagicMock(return_value=None)
    host.deliver = AsyncMock()
    workers.hosts["main"] = host

    write_marker(tmp_path, _marker(session_id="ghost"))
    await workers.resume_after_restart()  # must not raise

    host.deliver.assert_not_awaited()
    assert read_markers(tmp_path) == []


async def test_resume_noop_when_workers_disabled(tmp_path):
    workers = create_runtime_app(AppConfig()).state.claude_workers
    # No markers dir, workers disabled — must be a quiet no-op.
    await workers.resume_after_restart()


# --- CLI wiring ------------------------------------------------------------


def test_cli_writes_marker_and_triggers_detached_restart(tmp_path, monkeypatch):
    from agentdeck import __main__ as cli

    cfg = AppConfig.model_validate(
        {"claude_workers": {"enabled": True, "state_dir": str(tmp_path)}}
    )
    monkeypatch.setattr(cli, "load_config", lambda _p: cfg)
    monkeypatch.setattr(cli, "config_path", lambda: tmp_path / "config.toml")

    triggered: list[str] = []
    monkeypatch.setattr(
        "agentdeck.providers.claude_code.restart.trigger_detached_restart",
        lambda service: triggered.append(service),
    )

    cli._restart_runtime(
        [
            "--session",
            "sid-xyz",
            "--service",
            "agentdeck-staging-codex.service",
            "--then",
            "run pytest and report",
        ]
    )

    assert triggered == ["agentdeck-staging-codex.service"]
    entries = read_markers(tmp_path)
    assert len(entries) == 1
    _, marker = entries[0]
    assert marker.session_id == "sid-xyz"
    assert marker.prompt == "run pytest and report"


def test_cli_requires_a_session_id(tmp_path, monkeypatch):
    from agentdeck import __main__ as cli

    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    with pytest.raises(SystemExit):
        cli._restart_runtime(["--service", "agentdeck-staging-codex.service"])


def test_cli_refuses_autodetected_non_runtime_unit(tmp_path, monkeypatch):
    from agentdeck import __main__ as cli

    monkeypatch.delenv("AGENTDECK_RUNTIME_UNIT", raising=False)
    monkeypatch.setattr(
        "agentdeck.providers.claude_code.restart.detect_runtime_unit",
        lambda: "some-random.service",
    )
    with pytest.raises(SystemExit):
        cli._restart_runtime(["--session", "sid-xyz"])


def test_cli_drops_marker_if_restart_trigger_fails(tmp_path, monkeypatch):
    import subprocess

    from agentdeck import __main__ as cli

    cfg = AppConfig.model_validate(
        {"claude_workers": {"enabled": True, "state_dir": str(tmp_path)}}
    )
    monkeypatch.setattr(cli, "load_config", lambda _p: cfg)
    monkeypatch.setattr(cli, "config_path", lambda: tmp_path / "config.toml")

    def boom(_service):
        raise subprocess.CalledProcessError(1, "systemd-run")

    monkeypatch.setattr(
        "agentdeck.providers.claude_code.restart.trigger_detached_restart", boom
    )

    with pytest.raises(SystemExit):
        cli._restart_runtime(
            ["--session", "sid-xyz", "--service", "agentdeck-staging-codex.service"]
        )
    assert read_markers(tmp_path) == []  # marker cleaned up on failure
