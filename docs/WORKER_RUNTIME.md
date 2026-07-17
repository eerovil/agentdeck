# Deck-owned Claude workers

Agentdeck can own headless Claude Code worker processes: spawn them, steer them
mid-run, interrupt them, and revive finished sessions with context intact. The
intended caller is an external dispatcher (for example a board/issue poller)
that decides *what* work should exist; agentdeck owns *how* worker sessions run.

## Model

One worker = one long-lived `claude -p --input-format stream-json
--output-format stream-json` process. The stream-json channel gives us:

- `system/init` → the session id (also the transcript filename)
- user-message frames written to stdin mid-turn → delivered to the model at the
  next tool boundary (live steering, verified empirically)
- `control_request {subtype: interrupt}` → immediate turn abort with an ack
- `result` events → turn completion + subtype
- `--resume <session-id>` in a fresh process → full-context revival

Transcripts land in `<CLAUDE_CONFIG_DIR>/projects/`, so the ordinary
ClaudeCodeProvider scan displays deck-owned workers with no extra plumbing.

## The one primitive: `deliver(key, message)`

Workers are keyed by an opaque **dedupe key** chosen by the caller (e.g.
`kanban:owner/repo#123`). At most one live worker exists per key. `deliver`
is idempotent and picks the right mechanic itself:

| Worker state for key | Action |
|---|---|
| live, turn active | steer (message reaches the model mid-turn) |
| live, idle | queue as the next turn on the same process |
| exited, session known | revive: fresh process with `--resume` |
| unknown | spawn fresh (requires `cwd`) |
| revive fails (transcript gone, incompatible) | automatic fresh-spawn fallback |

`fresh: true` forces a clean-slate spawn for a poisoned session. Capacity
(`max_workers`, per account) applies only to (re)spawns — steering a live
worker is always allowed, so in-flight work can finish even at the cap.

Per-key state (`session_id`, cwd, last result) persists to a small JSON file so
revival works across runtime restarts. History lives in the transcripts.

## HTTP surface (runtime service)

Config-gated (`[claude_workers] enabled = true`), served by the long-lived
runtime service so web redeploys never kill workers:

```
GET    /claude/accounts/{label}/workers                  # registry snapshot
POST   /claude/accounts/{label}/deliver                  # {key, message, cwd?, fresh?}
POST   /claude/accounts/{label}/workers/{key}/interrupt
POST   /claude/accounts/{label}/workers/{key}/stop       # terminate process, keep lineage
DELETE /claude/accounts/{label}/workers/{key}            # forget a finished lineage
```

`deliver` responds `{accepted, action, reason?, session_id}` with action one of
`spawned | revived | steered | queued | rejected`. Callers need no other state:
poll the snapshot each cycle and re-`deliver` anything that was rejected.

## Configuration

```toml
[claude_workers]
enabled = true
max_workers = 4                 # per account; steering is exempt
permission_mode = "acceptEdits" # or "bypassPermissions" for fully autonomous workers
model = ""                      # "" = account default
state_dir = "~/.local/share/agentdeck/claude-workers"
```

Workers run under the account's `CLAUDE_CONFIG_DIR` (the `config_dir` of the
matching `[[accounts]]` entry), so per-account usage limits, skills, and
settings apply as they would to any session of that account.

## Roadmap

- Admission policy: refuse spawns when the account is over a usage ceiling
  (reusing the deck's per-account `UsageSnapshot` poller).
- Stalled-worker detection as lifecycle policy (idle/silent too long).
- Deck UI steering controls for owned workers (capabilities: STEER/INTERRUPT).
