import json
import stat
from pathlib import Path

from agentdeck.models import Account, Session, SessionStatus
from agentdeck.providers.claude_code import inject


def _account(root: Path) -> Account:
    return Account(key="claude_code:main", provider_id="claude_code", label="main", root=root)


def _session(cwd: Path | None) -> Session:
    return Session(
        key="claude_code:main:sid",
        account_key="claude_code:main",
        session_id="sid-123",
        status=SessionStatus.IDLE,
        cwd=cwd,
    )


def _trust(config_dir: Path, cwd: Path) -> None:
    (config_dir / ".claude.json").write_text(
        json.dumps({"projects": {str(cwd): {"hasTrustDialogAccepted": True}}})
    )


def _stub_claude(tmp_path: Path, *, exit_code: int = 0, out: str = "ok", err: str = "") -> str:
    """A fake `claude` that records argv+env+cwd and emits canned output."""
    script = tmp_path / "claude"
    record = tmp_path / "invocation.json"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, os, json\n"
        f"json.dump({{'argv': sys.argv[1:], 'cwd': os.getcwd(),"
        f" 'config_dir': os.environ.get('CLAUDE_CONFIG_DIR')}}, open({str(record)!r}, 'w'))\n"
        f"sys.stdout.write({out!r})\n"
        f"sys.stderr.write({err!r})\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRUSR)
    return str(script)


# --- preflight interlocks --------------------------------------------


def test_preflight_refuses_missing_cwd(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    reason = inject.preflight(_account(cfg), _session(None), proc_root=tmp_path / "proc")
    assert reason and "working directory" in reason


def test_preflight_refuses_nonexistent_cwd(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    gone = _session(tmp_path / "gone")
    reason = inject.preflight(_account(cfg), gone, proc_root=tmp_path / "proc")
    assert reason and "no longer exists" in reason


def test_preflight_refuses_untrusted(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    reason = inject.preflight(_account(cfg), _session(work), proc_root=tmp_path / "proc")
    assert reason and "not trusted" in reason


def test_preflight_ok_when_trusted_idle(tmp_path):
    cfg = tmp_path / "cfg"
    (cfg / "sessions").mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    _trust(cfg, work)
    assert inject.preflight(_account(cfg), _session(work), proc_root=tmp_path / "proc") is None


def test_preflight_refuses_live_session(tmp_path):
    cfg = tmp_path / "cfg"
    sessions = cfg / "sessions"
    sessions.mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    _trust(cfg, work)
    # a registry entry for our session id + a matching fake /proc starttime → "live"
    (sessions / "555.json").write_text(
        json.dumps({"pid": 555, "sessionId": "sid-123", "procStart": "77", "cwd": str(work)})
    )
    proc = tmp_path / "proc" / "555"
    proc.mkdir(parents=True)
    fields = [str(i) for i in range(30)]
    fields[0] = "S"
    fields[19] = "77"
    (proc / "stat").write_text("555 (claude) " + " ".join(fields))
    reason = inject.preflight(_account(cfg), _session(work), proc_root=tmp_path / "proc")
    assert reason and "live" in reason


# --- one-shot spawn --------------------------------------------------


async def test_inject_oneshot_success_passes_config_and_cwd(tmp_path):
    cfg = tmp_path / "cfg"
    (cfg / "sessions").mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    _trust(cfg, work)
    claude = _stub_claude(tmp_path, exit_code=0, out="did the thing")

    result = await inject.inject_oneshot(
        _account(cfg),
        _session(work),
        "hello session",
        claude_bin=claude,
        proc_root=tmp_path / "proc",
    )
    assert result.ok
    assert "did the thing" in result.detail
    rec = json.loads((tmp_path / "invocation.json").read_text())
    assert rec["argv"] == ["-p", "--resume", "sid-123", "hello session"]
    assert rec["cwd"] == str(work)
    assert rec["config_dir"] == str(cfg)


async def test_inject_oneshot_failure_returns_stderr(tmp_path):
    cfg = tmp_path / "cfg"
    (cfg / "sessions").mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    _trust(cfg, work)
    claude = _stub_claude(tmp_path, exit_code=3, out="", err="boom")

    result = await inject.inject_oneshot(
        _account(cfg), _session(work), "hi", claude_bin=claude, proc_root=tmp_path / "proc"
    )
    assert not result.ok
    assert result.exit_code == 3
    assert "boom" in result.detail


async def test_inject_oneshot_refuses_before_spawn(tmp_path):
    """A refused preflight must never spawn claude."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    claude = _stub_claude(tmp_path)
    result = await inject.inject_oneshot(
        _account(cfg), _session(None), "hi", claude_bin=claude, proc_root=tmp_path / "proc"
    )
    assert not result.ok
    assert not (tmp_path / "invocation.json").exists()  # never launched


async def test_inject_oneshot_empty_message(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    result = await inject.inject_oneshot(
        _account(cfg), _session(tmp_path), "   ", proc_root=tmp_path / "proc"
    )
    assert not result.ok
    assert "empty" in result.detail


def test_is_trusted_false_without_file(tmp_path):
    assert inject.is_trusted(tmp_path, tmp_path / "x") is False
