"""``python -m agentdeck`` / the ``agentdeck`` console script."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx
import uvicorn

from .app import create_app
from .config import config_path, load_config
from .providers.codex.runtime_client import runtime_socket_path


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "delegate":
        _delegate(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "codex-runtime":
        _codex_runtime(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "restart-runtime":
        _restart_runtime(sys.argv[2:])
        return

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Never emit bearer tokens even if something tries to log one.
    _install_redaction_filter()

    path = config_path()
    config = load_config(path)
    app = create_app(config)
    logging.getLogger(__name__).info(
        "agentdeck starting on %s:%d (%d account(s))",
        config.server.bind,
        config.server.port,
        len(config.accounts),
    )
    # Session SSE streams are intentionally long-lived and browsers reconnect
    # them automatically. Do not let those connections hold a frontend deploy
    # open until systemd's stop timeout; Codex work lives in the runtime service.
    uvicorn.run(
        app,
        host=config.server.bind,
        port=config.server.port,
        log_level="info",
        timeout_graceful_shutdown=2,
    )


def _codex_runtime(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="agentdeck codex-runtime",
        description="Run the persistent local Codex control service.",
    )
    parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _install_redaction_filter()
    from .runtime import create_runtime_app

    socket = runtime_socket_path()
    socket.parent.mkdir(parents=True, exist_ok=True)
    socket.unlink(missing_ok=True)
    os.umask(0o077)
    config = load_config(config_path())
    logging.getLogger(__name__).info("Codex runtime listening on %s", socket)
    uvicorn.run(create_runtime_app(config), uds=str(socket), log_level="info")


def _restart_runtime(argv: list[str]) -> None:
    import subprocess

    from .providers.claude_code.restart import (
        DEFAULT_CONTINUATION,
        RestartMarker,
        detect_runtime_unit,
        looks_like_runtime_unit,
        trigger_detached_restart,
        write_marker,
    )

    parser = argparse.ArgumentParser(
        prog="agentdeck restart-runtime",
        description=(
            "Restart the persistent runtime and continue this session afterward."
            " Use this instead of `systemctl restart` from inside an agent turn:"
            " it detaches the restart so it does not kill the caller, and"
            " re-drives this session once the fresh runtime is up."
        ),
    )
    parser.add_argument(
        "--then",
        dest="prompt",
        default=DEFAULT_CONTINUATION,
        help="follow-up instruction delivered to this session after the restart",
    )
    parser.add_argument(
        "--session",
        default=os.environ.get("CLAUDE_CODE_SESSION_ID"),
        help="session id to continue (defaults to $CLAUDE_CODE_SESSION_ID)",
    )
    parser.add_argument(
        "--service",
        default=os.environ.get("AGENTDECK_RUNTIME_UNIT"),
        help="systemd --user unit to restart (auto-detected from the cgroup if unset)",
    )
    args = parser.parse_args(argv)

    session_id = (args.session or "").strip()
    if not session_id:
        parser.error(
            "no session id: run this from inside an agent turn (so"
            " $CLAUDE_CODE_SESSION_ID is set) or pass --session"
        )

    explicit_service = args.service is not None
    service = (args.service or detect_runtime_unit() or "").strip()
    if not service:
        parser.error("could not determine the runtime unit; pass --service")
    if not explicit_service and not looks_like_runtime_unit(service):
        parser.error(
            f"refusing to auto-restart {service!r}: it does not look like an"
            " agentdeck runtime unit; pass --service explicitly if intended"
        )

    config = load_config(config_path())
    state_dir = config.claude_workers.state_path

    marker = RestartMarker(
        session_id=session_id,
        prompt=args.prompt,
        service=service,
        created_at=time.time(),
    )
    marker_path = write_marker(state_dir, marker)
    print(
        f"Restarting {service}; this session will resume automatically once it is"
        " back up.",
        file=sys.stderr,
    )
    try:
        trigger_detached_restart(service)
    except (OSError, subprocess.CalledProcessError) as exc:
        # No restart happened, so the marker would never be consumed — drop it.
        marker_path.unlink(missing_ok=True)
        parser.exit(1, f"could not trigger restart: {exc}\n")


def _delegate(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="agentdeck delegate",
        description="Delegate a stdin prompt to an AgentDeck-owned Codex chat.",
    )
    parser.add_argument("--cwd", default=os.getcwd(), help="Codex working directory")
    parser.add_argument("--account", help="AgentDeck account key; auto-selected if unique")
    parser.add_argument(
        "--sandbox",
        choices=("read-only", "workspace-write"),
        default="workspace-write",
    )
    parser.add_argument("--model", help="Codex model override")
    parser.add_argument(
        "--approval-policy",
        choices=("untrusted", "on-failure", "on-request", "never"),
        help="Codex approval policy; use 'never' for a fully autonomous run"
        " (no per-command prompts, e.g. an unattended kanban worker)",
    )
    parser.add_argument(
        "--parent-session",
        default=os.environ.get("CLAUDE_CODE_SESSION_ID"),
        help="id of the session initiating this delegation (defaults to"
        " $CLAUDE_CODE_SESSION_ID); lets AgentDeck nest the delegated chat"
        " under its parent",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("AGENTDECK_URL", "http://127.0.0.1:8756"),
        help="running AgentDeck base URL",
    )
    parser.add_argument("--poll-interval", type=float, default=1.0)
    args = parser.parse_args(argv)

    message = sys.stdin.read()
    if not message.strip():
        parser.error("a prompt must be supplied on stdin")
    try:
        cwd = str(Path(args.cwd).expanduser().resolve(strict=True))
    except (OSError, RuntimeError) as exc:
        parser.error(f"invalid working directory: {exc}")

    base_url = args.url.rstrip("/") + "/"
    payload = {"cwd": cwd, "message": message, "sandbox": args.sandbox}
    if args.account:
        payload["account_key"] = args.account
    if args.model:
        payload["model"] = args.model
    if args.approval_policy:
        payload["approval_policy"] = args.approval_policy
    if args.parent_session:
        payload["parent_session_id"] = args.parent_session

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(urljoin(base_url, "api/delegations"), json=payload)
            response.raise_for_status()
            started = response.json()
            status_url = urljoin(base_url, started["status_url"].lstrip("/"))
            delegation_id = started["id"]
            print(f"AgentDeck delegation {delegation_id} started.", file=sys.stderr)
            previous = None
            while True:
                response = client.get(status_url)
                response.raise_for_status()
                status = response.json()
                state = status["state"]
                if state != previous:
                    previous = state
                    detail = f" ({status['session_url']})" if status.get("session_url") else ""
                    print(f"AgentDeck delegation: {state}{detail}", file=sys.stderr)
                    if state == "waiting" and status.get("interaction"):
                        print(
                            "Codex needs input in AgentDeck: "
                            + json.dumps(status["interaction"], ensure_ascii=False),
                            file=sys.stderr,
                        )
                if state == "complete":
                    final_message = status.get("final_message")
                    if final_message:
                        print(final_message)
                    return
                if state == "failed":
                    parser.exit(1, f"AgentDeck delegation failed: {status.get('reason')}\n")
                time.sleep(max(0.1, args.poll_interval))
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        parser.exit(1, f"AgentDeck delegation request failed: {exc}\n")


def _install_redaction_filter() -> None:
    import re

    pattern = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+|sk-ant-[A-Za-z0-9._\-]+")

    class _Redact(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            if isinstance(record.msg, str):
                record.msg = pattern.sub(r"\g<1>[redacted]", record.msg)
            return True

    logging.getLogger().addFilter(_Redact())


if __name__ == "__main__":
    main()
