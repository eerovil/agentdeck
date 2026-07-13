from __future__ import annotations

import io

import agentdeck.__main__ as cli


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Client:
    def __init__(self):
        self.posted = None
        self.polls = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def post(self, url, *, json):
        self.posted = (url, json)
        return _Response(
            {
                "id": "delegation-1",
                "status_url": "/api/delegations/delegation-1",
            }
        )

    def get(self, url):
        self.polls += 1
        if self.polls == 1:
            return _Response(
                {
                    "state": "running",
                    "session_url": "/sessions/codex:test:thread-1",
                }
            )
        return _Response(
            {
                "state": "complete",
                "session_url": "/sessions/codex:test:thread-1",
                "final_message": "Codex finished the delegated task.",
            }
        )


def test_delegate_cli_sends_stdin_and_prints_final_message(tmp_path, monkeypatch, capsys):
    client = _Client()
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("Inspect the change.\n"))
    monkeypatch.setattr(cli.time, "sleep", lambda delay: None)

    cli._delegate(
        [
            "--cwd",
            str(tmp_path),
            "--sandbox",
            "read-only",
            "--model",
            "gpt-test",
        ]
    )

    captured = capsys.readouterr()
    assert captured.out == "Codex finished the delegated task.\n"
    assert "delegation-1 started" in captured.err
    assert client.posted == (
        "http://127.0.0.1:8756/api/delegations",
        {
            "cwd": str(tmp_path),
            "message": "Inspect the change.\n",
            "sandbox": "read-only",
            "model": "gpt-test",
        },
    )
