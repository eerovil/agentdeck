import json

from agentdeck.providers.claude_code import transcripts


def _write(path, lines):
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))


ASSISTANT = {
    "type": "assistant",
    "timestamp": "2026-07-03T08:00:01Z",
    "message": {
        "role": "assistant",
        "model": "claude-opus-4-8",
        "content": [
            {"type": "text", "text": "Doing the thing"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
        ],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 5,
            "cache_creation_input_tokens": 3,
        },
    },
}
USER = {"type": "user", "message": {"role": "user", "content": "hello there"}}
TOOL_RESULT = {
    "type": "user",
    "message": {"role": "user", "content": [{"type": "tool_result", "content": "file listing"}]},
}


def test_read_events_parses_roles(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [USER, ASSISTANT, TOOL_RESULT])
    read = transcripts.read_events(p)
    assert [e.role for e in read.events] == ["user", "assistant", "tool"]
    a = read.events[1]
    assert a.text == "Doing the thing"
    assert a.tool_name == "Bash"
    assert "ls -la" in a.tool_summary
    assert a.model == "claude-opus-4-8"


def test_read_events_skips_malformed_and_meta(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(
        json.dumps(USER) + "\n" + "{ broken\n" + json.dumps({"type": "mode", "mode": "x"}) + "\n"
    )
    read = transcripts.read_events(p)
    assert read.skipped == 1
    assert [e.role for e in read.events] == ["user"]  # mode line yields nothing


def test_read_events_incremental_and_partial_trailing(tmp_path):
    p = tmp_path / "t.jsonl"
    # two complete lines + a partial (no trailing newline)
    p.write_text(json.dumps(USER) + "\n" + json.dumps(ASSISTANT) + "\n" + '{"type":"user","mess')
    read1 = transcripts.read_events(p)
    assert len(read1.events) == 2
    # the partial line must not have been consumed
    # complete the file and resume from the cursor
    with p.open("a") as f:
        f.write('age":{"role":"user","content":"more"}}\n')
    read2 = transcripts.read_events(p, byte_offset=read1.byte_offset, seq=read1.seq)
    assert [e.text for e in read2.events] == ["more"]
    assert read2.events[0].seq == 3  # seq continued past the first two lines


def test_token_totals(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [ASSISTANT, ASSISTANT])
    read = transcripts.read_events(p)
    tot = transcripts.token_totals(read.events)
    assert tot.input_tokens == 200
    assert tot.output_tokens == 40
    assert tot.cache_read_tokens == 10
    assert tot.total == 256


def test_last_model(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [USER, ASSISTANT])
    read = transcripts.read_events(p)
    assert transcripts.last_model(read.events) == "claude-opus-4-8"


def test_load_todos(tmp_path):
    cfg = tmp_path / "cfg"
    tdir = cfg / "tasks" / "sid-1"
    tdir.mkdir(parents=True)
    (tdir / "0.json").write_text(json.dumps({"subject": "do X", "status": "in_progress"}))
    (tdir / "1.json").write_text(json.dumps([{"subject": "do Y", "status": "completed"}]))
    todos = transcripts.load_todos(cfg, "sid-1")
    assert {t["subject"] for t in todos} == {"do X", "do Y"}


def test_load_todos_missing(tmp_path):
    assert transcripts.load_todos(tmp_path, "nope") == []


def test_transcript_meta_prefers_ai_title(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(
        p,
        [
            {"type": "ai-title", "aiTitle": "Fix the login bug", "sessionId": "s"},
            {"type": "last-prompt", "lastPrompt": "now add a test", "sessionId": "s"},
            USER,
        ],
    )
    title, last_prompt, first_user, cwd = transcripts.transcript_meta(p)
    assert title == "Fix the login bug"
    assert last_prompt == "now add a test"
    assert first_user == "hello there"


def test_transcript_meta_first_user_fallback(tmp_path):
    p = tmp_path / "t.jsonl"
    meta_line = {"type": "user", "isMeta": True, "message": {"role": "user", "content": "META"}}
    _write(p, [meta_line, USER])
    title, last_prompt, first_user, cwd = transcripts.transcript_meta(p)
    assert title is None  # no ai-title
    assert first_user == "hello there"  # meta line skipped


def test_transcript_meta_extracts_cwd(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(
        p,
        [
            {
                "type": "user",
                "cwd": "/var/home/eero/outdoor",
                "message": {"role": "user", "content": "hi"},
            },
        ],
    )
    _title, _lp, _fu, cwd = transcripts.transcript_meta(p)
    assert cwd == "/var/home/eero/outdoor"


def test_transcript_meta_missing_file(tmp_path):
    assert transcripts.transcript_meta(tmp_path / "nope.jsonl") == (None, None, None, None)
