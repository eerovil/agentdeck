import asyncio
import stat
from pathlib import Path

import pytest

from agentdeck.chat import ChatManager
from agentdeck.providers.claude_code.chat import ChatSession, normalize_stream_event


def test_normalize_assistant_text():
    obj = {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi there"}]}}
    evs = normalize_stream_event(obj)
    assert evs == [{"role": "assistant", "text": "hi there"}]


def test_normalize_tool_use():
    obj = {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]},
    }
    (ev,) = normalize_stream_event(obj)
    assert ev["role"] == "tool"
    assert ev["tool_name"] == "Bash"
    assert "ls" in ev["tool_summary"]


def test_normalize_result_is_turn_end():
    assert normalize_stream_event({"type": "result", "subtype": "success"}) == [
        {"role": "system", "event": "turn-end"}
    ]


def test_normalize_ignores_system():
    assert normalize_stream_event({"type": "system", "subtype": "init"}) == []


def _stub_chat_claude(tmp_path: Path) -> str:
    """A fake stream-json `claude`: echoes each user message as an assistant turn."""
    script = tmp_path / "claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "print(json.dumps({'type':'system','subtype':'init'}), flush=True)\n"
        "while True:\n"
        "    line = sys.stdin.readline()\n"
        "    if not line: break\n"
        "    line = line.strip()\n"
        "    if not line: continue\n"
        "    try: msg = json.loads(line)\n"
        "    except Exception: continue\n"
        "    c = msg.get('message', {}).get('content') or []\n"
        "    txt = ' '.join(b.get('text','') for b in c if b.get('type')=='text')\n"
        "    block = {'type':'text','text':'echo: '+txt}\n"
        "    reply = {'type':'assistant','message':{'content':[block]}}\n"
        "    print(json.dumps(reply), flush=True)\n"
        "    print(json.dumps({'type':'result','subtype':'success'}), flush=True)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRUSR)
    return str(script)


async def test_chatsession_echo_roundtrip(tmp_path):
    stub = _stub_chat_claude(tmp_path)
    cs = ChatSession("sid-1", cwd=str(tmp_path), config_dir=str(tmp_path), claude_bin=stub)
    await cs.start()
    try:
        await cs.send("hello")
        got = []

        async def collect():
            async for _, ev in cs.stream(0):
                got.append(ev)
                if ev.get("event") == "turn-end":
                    return

        await asyncio.wait_for(collect(), timeout=10.0)
    finally:
        await cs.stop()

    texts = [e.get("text") for e in got if e.get("text")]
    assert "hello" in texts  # our own message echoed into the log
    assert any("echo: hello" in t for t in texts)  # stub's assistant reply
    assert cs.owned_pid is not None


async def test_chatsession_send_after_close_is_false(tmp_path):
    stub = _stub_chat_claude(tmp_path)
    cs = ChatSession("sid-2", cwd=str(tmp_path), config_dir=str(tmp_path), claude_bin=stub)
    await cs.start()
    await cs.stop()
    assert await cs.send("nope") is False


# --- manager -----------------------------------------------------------


class _FakeChat:
    def __init__(self):
        self.closed = False
        self.stopped = False

    async def stop(self):
        self.stopped = True
        self.closed = True


class _FakeProvider:
    def __init__(self):
        self.opens = 0

    async def open_chat(self, account, session):
        self.opens += 1
        return _FakeChat()


async def test_manager_reuses_open_chat():
    mgr = ChatManager()
    prov = _FakeProvider()
    a = await mgr.get_or_open("k", None, None, prov)
    b = await mgr.get_or_open("k", None, None, prov)
    assert a is b
    assert prov.opens == 1


async def test_manager_stop_removes_and_stops():
    mgr = ChatManager()
    prov = _FakeProvider()
    cs = await mgr.get_or_open("k", None, None, prov)
    await mgr.stop("k")
    assert cs.stopped
    assert mgr.get("k") is None


async def test_manager_reopens_after_close():
    mgr = ChatManager()
    prov = _FakeProvider()
    cs = await mgr.get_or_open("k", None, None, prov)
    cs.closed = True  # child died
    again = await mgr.get_or_open("k", None, None, prov)
    assert again is not cs
    assert prov.opens == 2


@pytest.mark.parametrize("event,expect", [
    ({"type": "assistant", "message": {"content": []}}, []),
    ({"type": "nonsense"}, []),
])
def test_normalize_edge(event, expect):
    assert normalize_stream_event(event) == expect
