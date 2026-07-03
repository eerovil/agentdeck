from agentdeck.providers.claude_code import registry


def test_read_registry_parses_and_skips_malformed(fake_config_dir):
    entries = registry.read_registry(fake_config_dir.path)
    assert len(entries) == 1  # the malformed bad.json is skipped
    e = entries[0]
    assert e.pid == 4242
    assert e.session_id == fake_config_dir.live_sid
    assert str(e.cwd) == "/tmp/proj"
    assert e.proc_start == "19"
    assert e.kind == "interactive"


def test_read_registry_missing_dir(tmp_path):
    assert registry.read_registry(tmp_path / "nope") == []


def test_is_alive_matching_starttime(fake_config_dir, make_proc):
    make_proc(4242, "19")
    (e,) = registry.read_registry(fake_config_dir.path)
    assert registry.is_alive(e, proc_root=make_proc.root) is True


def test_is_alive_dead_when_pid_absent(fake_config_dir, make_proc):
    # no proc entry created
    (e,) = registry.read_registry(fake_config_dir.path)
    assert registry.is_alive(e, proc_root=make_proc.root) is False


def test_is_alive_dead_on_pid_reuse(fake_config_dir, make_proc):
    make_proc(4242, "99999")  # pid exists but different starttime → reused
    (e,) = registry.read_registry(fake_config_dir.path)
    assert registry.is_alive(e, proc_root=make_proc.root) is False


def test_proc_starttime_none_when_missing(make_proc):
    assert registry.proc_starttime(12345, proc_root=make_proc.root) is None
