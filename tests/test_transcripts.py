import json

from agentdeck.providers.claude_code import transcripts


def _write(path, lines):
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))


def test_trailing_question():
    tq = transcripts.trailing_question
    # Picks the question sentence even when a statement follows.
    assert tq("Sounds good. Should I push both commits? Let me know.") == (
        "Should I push both commits?")
    # Last question wins when there are several.
    assert tq("Do you want A? Or would B be better?") == "Or would B be better?"
    # Newlines are flattened.
    assert tq("Here is the plan.\n\nWant me to proceed with it?") == (
        "Want me to proceed with it?")
    # A bulleted / colon block (no full stops) must not swallow the whole thing
    # into one giant "sentence" — the trailing line is the question.
    recap = "Recap of what shipped:\n- kanban titles\n- colour stripes\n\nWhat's next?"
    assert tq(recap) == "What's next?"
    # No question → None.
    assert tq("All done, pushed to master.") is None
    assert tq("") is None
    assert tq(None) is None
    # Guards: a URL query or a ternary must not register as a question.
    assert tq("Fetching /static/app.css?v=abc now.") is None
    assert tq("The value is a ? b : c in that branch.") is None


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


def _enqueue(text):
    return {"type": "queue-operation", "operation": "enqueue", "content": text}


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


def test_queued_message_rendered_as_user(tmp_path):
    """A message typed while busy (queue-operation/enqueue) that never became a
    real user turn still shows, flagged as queued."""
    p = tmp_path / "t.jsonl"
    _write(p, [USER, _enqueue("sent while you were working"), ASSISTANT])
    read = transcripts.read_events(p)
    queued = [e for e in read.events if e.queued]
    assert len(queued) == 1
    assert queued[0].role == "user"
    assert queued[0].text == "sent while you were working"


def test_queued_duplicate_deduped(tmp_path):
    """If the enqueued message was later processed as a real user turn, only the
    real turn is kept (no double render); non-enqueue ops are ignored."""
    p = tmp_path / "t.jsonl"
    _write(
        p,
        [
            _enqueue("do the thing"),
            {"type": "queue-operation", "operation": "dequeue"},
            {"type": "user", "message": {"role": "user", "content": "do the thing"}},
        ],
    )
    read = transcripts.read_events(p)
    texts = [(e.text, e.queued) for e in read.events]
    assert texts == [("do the thing", False)]  # the real turn, once


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
    title, last_prompt, first_user, cwd, _last_text, _last_role = transcripts.transcript_meta(p)
    assert title == "Fix the login bug"
    # the USER line is newer than the last-prompt bookkeeping, so it wins
    assert last_prompt == "hello there"
    assert first_user == "hello there"


def test_last_prompt_tracks_user_line_not_just_bookkeeping(tmp_path):
    """The card's last_prompt must follow the real user line, which is written
    before the lagging `last-prompt` bookkeeping — else the list shows a stale
    prompt while the agent is already working on the new one."""
    p = tmp_path / "t.jsonl"
    _write(
        p,
        [
            {"type": "last-prompt", "lastPrompt": "old question", "sessionId": "s"},
            {"type": "user", "message": {"role": "user", "content": "the NEW question"}},
            # agent has started; the new `last-prompt` line hasn't been written yet
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "on it"}]},
            },
        ],
    )
    _title, last_prompt, _fu, _cwd, _lt, _lr = transcripts.transcript_meta(p)
    assert last_prompt == "the NEW question"


def test_transcript_meta_first_user_fallback(tmp_path):
    p = tmp_path / "t.jsonl"
    meta_line = {"type": "user", "isMeta": True, "message": {"role": "user", "content": "META"}}
    _write(p, [meta_line, USER])
    title, last_prompt, first_user, cwd, _last_text, _last_role = transcripts.transcript_meta(p)
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
    _title, _lp, _fu, cwd, _lt, _lr = transcripts.transcript_meta(p)
    assert cwd == "/var/home/eero/outdoor"


def test_transcript_meta_missing_file(tmp_path):
    assert transcripts.transcript_meta(tmp_path / "nope.jsonl") == (
        None, None, None, None, None, None,
    )


def test_transcript_meta_extracts_last_agent_text(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(
        p,
        [
            USER,
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "older"}]},
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "newest reply"}],
                },
            },
        ],
    )
    _at, _lp, _fu, _cwd, last_text, _lr = transcripts.transcript_meta(p)
    assert last_text == "newest reply"


def test_last_role_reflects_most_recent_message(tmp_path):
    """last_role tells the card which line is newest. A user turn after the
    agent's reply (agent working, no new text yet) must read as 'user'."""
    agent = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "here you go"}]},
    }
    # agent replied, then the user sent a new message (agent not yet answered)
    newq = {"type": "user", "message": {"role": "user", "content": "and now this"}}
    p = tmp_path / "u.jsonl"
    _write(p, [USER, agent, newq])
    assert transcripts.transcript_meta(p)[5] == "user"
    # agent replied last
    p2 = tmp_path / "a.jsonl"
    _write(p2, [USER, agent])
    assert transcripts.transcript_meta(p2)[5] == "agent"
