# Codex session injection plan

Status: implemented, deployed, enabled in the live configuration, and verified
end to end against a disposable completed Codex `exec` session.

## Goal

Allow an agentdeck user to send a follow-up into an existing Codex session
without allowing two independent Codex processes to write the same active
rollout. Keep the action disabled by default.

## Safety spike and resulting scope

The implementation began with the required concurrency spike against Codex CLI
0.144.3. It established two important facts:

1. A second app-server reported an actively running thread owned by another
   Codex instance as `idle`. App-server ownership is instance-local.
2. `codex exec resume` accepted a second turn while the first process was in a
   long-running tool call. Both writers appended interleaved events to the same
   rollout instead of refusing the second writer.

Therefore neither app-server status nor rollout mtime is a safe ownership check
for arbitrary local sessions. A quiet TUI may remain open indefinitely, and a
non-interactive turn may be silent during a long tool call.

The safe first release is deliberately narrower:

- Only rollouts whose native source is `exec` are injectable.
- Their most recent native turn boundary must be `task_complete`.
- A later `task_started` or `turn_aborted` makes the rollout non-injectable.
- TUI and other session kinds are always read-only in agentdeck.
- TUI sessions remain visible for monitoring but expose no write controls or
  external-open button.

This uses a durable turn boundary rather than the provider's 30-second
display-liveness heuristic. The provider repeats the boundary check immediately
before spawning Codex.

## Implemented architecture

### Provider-neutral contract

`models.py` defines `Capability.INJECT` and `InjectResult`. `SessionProvider`
has optional `inject()` and `start_session()` hooks plus a provider-neutral
`supports_new_session` flag. The web layer checks capabilities/hooks and never
branches on provider ID.

### Codex eligibility and execution

`providers/codex/transcripts.py` reads the bounded transcript tail and exposes
`last_turn_complete()`.

`providers/codex/inject.py` accepts only a completed `exec` rollout, checks the
working directory, and starts:

```text
codex exec resume <session-id> - --json --skip-git-repo-check
```

The message is written through stdin and never appears in a shell command or
process argument. `CODEX_HOME` and cwd come from the resolved account/session,
not the browser. The process runs in its own process group and is terminated as
a group on cancellation or timeout. Stdout and stderr are discarded because
the existing rollout tail is the canonical response stream.

### Coordination and lifecycle

`InjectionService` owns background turns and an in-memory status registry. It:

- serializes submissions per session in a FIFO queue;
- validates the global switch and message length;
- reports running, complete, or failed status;
- cancels and reaps owned children during application shutdown;
- never persists submitted message text outside the Codex rollout.

### Configuration

```toml
[inject]
enabled = false
timeout_s = 900
max_message_chars = 16000
```

Injection remains disabled by default. Anyone who can reach an enabled action
route can cause Codex to execute work on the host, so it must only be enabled on
a trusted bind.

### Web action

```text
POST /sessions/{session_key}/inject
GET  /partials/sessions/{session_key}/inject-status
POST /sessions/new
GET  /partials/new-session-status
```

The POST route:

- uses the central access dependency;
- rejects cross-site origins;
- resolves the account, session, cwd, and provider server-side;
- returns 403 when disabled or incapable, 422 for invalid text, and 202 when
  queued;
- returns escaped HTMX fragments rather than subprocess output.

The detail-page composer appears only when the switch is enabled and the
session currently has `Capability.INJECT`, or while agentdeck owns an active
queue for it. Enter submits, Shift+Enter inserts a newline, and the status
fragment shows queued/running/completed messages while polling. The dashboard
offers a provider-neutral new-session form for providers that support it.
Transcript output continues to arrive through the existing per-session SSE
tail.

## Tests

Synthetic tests cover:

- completed, active, wrong-kind, and malformed rollout eligibility;
- prompt delivery through stdin and absence from argv;
- `CODEX_HOME`, cwd, and process-group spawn parameters;
- the spawn-time turn-boundary recheck;
- per-session serialization and shutdown cleanup;
- disabled, invalid, cross-origin, accepted, and queued web requests;
- new-session process arguments, route behavior, and Enter-to-send UI;
- injection capability discovery without controls on TUI sessions;
- weekly usage and the existing provider/web suites as regression coverage.

Manual verification uses only a disposable Codex session. It must confirm the
injected turn appears once in the existing rollout and that a TUI session never
shows the composer.

## Acceptance criteria

- No injection decision uses rollout recency as its ownership proof.
- TUI and active/incomplete exec rollouts never expose `Capability.INJECT`.
- Eligibility is rechecked immediately before process creation.
- Agentdeck runs at most one owned turn per session and queues later messages.
- Messages are passed over stdin, never interpolated into a shell or argv.
- Timeout/shutdown terminates only the process group created by agentdeck.
- Injection is disabled by default and blocked server-side when disabled.
- The existing transcript SSE renders the response without a duplicate output
  channel.
- Lint and the complete automated test suite pass.

## Future work

- Authoritative shared-daemon ownership, if Codex exposes cross-client thread
  ownership in a stable protocol.
- Active-turn steering as a separately labelled action.
- Explicit approval and structured user-input UI.
- Attachments and full interactive streaming chat.

## Sources and compatibility

- Protocol behavior was checked from the installed CLI schema generated with
  `codex app-server generate-json-schema --experimental`.
- Resume behavior was checked with `codex exec resume --help` and the disposable
  concurrency spike described above.
- Codex internals remain version-sensitive. Unknown session sources or turn
  boundaries fail closed and expose no injection capability.
