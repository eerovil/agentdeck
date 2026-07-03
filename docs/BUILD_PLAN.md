# agentdeck — Build Plan

Self-hosted, mobile-first dashboard for monitoring and steering coding-agent CLI sessions. v1: Claude Code. Architecture: provider abstraction over "session sources".

**License: Apache-2.0.** Rationale: explicit patent grant, NOTICE file mechanism, unambiguous corporate-friendliness, and a clean line against the AGPL `claudecodeui` project — no code, templates, or CSS may be copied from it; clean-room only.

---

## 0. Grounding facts (verified on the target host, Claude Code v2.1.198)

- Multi-account = iterate `CLAUDE_CONFIG_DIR`s (host has `~/.claude` = "main" and `~/.claude2` = "alt"). Each dir has its own `projects/`, `sessions/`, `history.jsonl`, `.credentials.json`.
- Live session discovery: `$CONFIG_DIR/sessions/<pid>.json` registry files `{pid, sessionId, cwd, startedAt, procStart, version, kind, entrypoint}`; verify liveness via `/proc/<pid>/stat` field 22 (starttime) == recorded `procStart` (pid-reuse guard). Registry files linger after process death. Remote-control/cloud-attached workers (`--sdk-url .../cse_*`) also register here and write local transcripts.
- Session data: transcripts at `$CONFIG_DIR/projects/<cwd-slug>/<uuid>.jsonl` (assistant lines carry full token `usage` blocks incl. cache splits + `model`), titles/last-prompts from `$CONFIG_DIR/history.jsonl` (`{display, project, sessionId, timestamp}`), subagent transcripts under `<uuid>/subagents/`, todos under `$CONFIG_DIR/tasks/<sessionId>/`.
- Usage limits: `GET https://api.anthropic.com/api/oauth/usage` with Bearer token read **fresh** from `$CONFIG_DIR/.credentials.json` (mode 600; tokens rotate) + header `anthropic-beta: oauth-2025-04-20` + a Claude-Code-style User-Agent → returns `five_hour`/`seven_day` utilization + `resets_at`. Server-side rate-limited: poll ~5 min per account, cache, back off on 429.
- Existing host script `outdoor/Docker/bin/kanban_poll.sh` runs `claude -p "/usage"` (client-side, not the HTTP endpoint) every ≤150s and caches at `/tmp/kanban-poll.usage` — agentdeck publishes a shared cache it can later migrate to.
- Message injection: idle sessions via `claude -p --resume <uuid> "<msg>"` (correct `CLAUDE_CONFIG_DIR` + cwd; trust-dialog gotcha: `$CFG/.claude.json` `projects[cwd].hasTrustDialogAccepted` must be true). Interactive attach via `claude -p --resume <id> --input-format stream-json --output-format stream-json --include-partial-messages` as a long-lived child. LIVE sessions: no local API — render a claude.ai/code deep-link. Never resume a session whose pid is live (single-writer JSONL).
- Routines: definitions cloud-only; runs landing on remote-control bridge envs appear as normal local sessions. Deep-link the rest.

---

## 1. Repo layout

Local checkout: `/var/home/eero/agentdeck`. GitHub: `eerovil/agentdeck` (public, personal account).

```
agentdeck/
├── pyproject.toml                  # uv-managed; hatchling build backend
├── uv.lock
├── LICENSE                         # Apache-2.0
├── NOTICE
├── README.md
├── CHANGELOG.md
├── .gitignore
├── config.example.toml             # committed; real config lives OUTSIDE the repo
├── systemd/
│   └── agentdeck.service           # user unit, installed to ~/.config/systemd/user/
├── docs/
│   ├── BUILD_PLAN.md               # this file
│   ├── providers.md                # how to write a new provider (v0.4)
│   └── claude-code-internals.md    # documents the undocumented surfaces we depend on
├── src/agentdeck/
│   ├── __init__.py                 # __version__
│   ├── __main__.py                 # python -m agentdeck → uvicorn runner
│   ├── app.py                      # FastAPI app factory, lifespan (start/stop collectors)
│   ├── config.py                   # TOML load + pydantic-settings models
│   ├── models.py                   # provider-neutral dataclasses (Account, Session, …)
│   ├── state.py                    # AppState: in-memory session/usage registry
│   ├── events.py                   # EventBus: asyncio pub/sub feeding SSE
│   ├── db.py                       # optional SQLite (usage history, sessions seen)
│   ├── collector.py                # per-account watch + scan orchestration
│   ├── providers/
│   │   ├── __init__.py             # PROVIDERS registry: {"claude_code": ClaudeCodeProvider}
│   │   ├── base.py                 # SessionProvider ABC + Capability enum
│   │   └── claude_code/
│   │       ├── __init__.py
│   │       ├── provider.py         # ClaudeCodeProvider (implements base)
│   │       ├── registry.py         # sessions/<pid>.json parsing + /proc liveness
│   │       ├── transcripts.py      # projects/<slug>/<uuid>.jsonl incremental parser
│   │       ├── history.py          # history.jsonl → titles / last prompts
│   │       ├── credentials.py      # fresh-read .credentials.json (never cached long)
│   │       ├── usage.py            # OAuth usage endpoint poller + backoff + shared cache
│   │       └── inject.py           # one-shot resume + stream-json chat children
│   └── web/
│       ├── __init__.py
│       ├── deps.py                 # request-scoped access to AppState/config + require_access stub
│       ├── routes_pages.py         # full-page GET routes
│       ├── routes_partials.py      # HTMX fragment GET routes
│       ├── routes_sse.py           # /events streams
│       ├── routes_actions.py       # POST inject/chat routes
│       ├── templates/
│       │   ├── base.html           # viewport meta, htmx + sse ext, limit-bar header
│       │   ├── dashboard.html
│       │   ├── session.html
│       │   ├── chat.html
│       │   └── partials/
│       │       ├── limit_bars.html
│       │       ├── session_list.html
│       │       ├── session_card.html
│       │       ├── transcript.html
│       │       ├── transcript_event.html
│       │       └── chat_message.html
│       └── static/
│           ├── app.css             # hand-written, mobile-first; no framework
│           ├── htmx.min.js         # vendored (pin version)
│           └── sse.js              # htmx SSE extension, vendored
└── tests/
    ├── conftest.py                 # fixture-configdir builders (tmp_path)
    ├── fixtures/
    │   ├── README.md               # "SYNTHETIC DATA ONLY — never copy real transcripts"
    │   └── claude_config_a/        # hand-written minimal config dir
    ├── test_registry.py
    ├── test_transcripts.py
    ├── test_history.py
    ├── test_usage.py
    ├── test_collector.py
    ├── test_inject.py
    └── test_web.py
```

**pyproject.toml essentials**

```toml
[project]
name = "agentdeck"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115", "uvicorn[standard]>=0.30", "jinja2>=3.1",
  "httpx>=0.27", "watchfiles>=0.22", "pydantic-settings>=2",
  "python-multipart",            # form posts
]
[project.scripts]
agentdeck = "agentdeck.__main__:main"
[dependency-groups]
dev = ["pytest", "pytest-asyncio", "respx", "ruff"]
```

**`.gitignore` essentials** (public-repo hygiene is a feature):

```gitignore
# secrets & local state — NEVER commit
config.toml
*.credentials.json
.credentials.json
*.db
*.db-wal
*.db-shm
.env
# real Claude data must never enter the tree
.claude/
projects/
history.jsonl
sessions/
# python
.venv/
__pycache__/
dist/
*.egg-info/
```

Plus a pre-commit-friendly guard (documented in README): `git grep -I "sk-ant\|oauth" -- ':!docs'` should return nothing; consider `gitleaks` in CI later.

**systemd/agentdeck.service** (rootless Bazzite, `systemd --user`):

```ini
[Unit]
Description=agentdeck dashboard
After=network-online.target tailscaled.service

[Service]
ExecStart=%h/.local/bin/uv run --directory %h/agentdeck agentdeck
Restart=on-failure
RestartSec=5
Environment=AGENTDECK_CONFIG=%h/.config/agentdeck/config.toml
# hygiene: keep child claude processes in our cgroup so stop kills chat children
KillMode=control-group

[Install]
WantedBy=default.target
```

Install: `systemctl --user enable --now agentdeck` (+ `loginctl enable-linger <user>`; document it).

**README skeleton sections**: What is agentdeck (screenshot) / Status & scope (v1 = Claude Code) / Security model (Tailscale-bind, no auth, threat model, why you must not bind 0.0.0.0 on untrusted LANs) / Install (uv + systemd user unit) / Configuration (config.toml reference) / How it works (undocumented surfaces disclaimer + link to docs/claude-code-internals.md) / Multi-account / Message injection & safety interlocks / Roadmap / Contributing (clean-room note re AGPL projects) / License.

---

## 2. Core data model + provider abstraction

All in `src/agentdeck/models.py` — provider-neutral; providers translate into these.

```python
class SessionStatus(StrEnum):
    LIVE = "live"        # owning process running locally (TUI / RC bridge) — read-only + deep-link
    IDLE = "idle"        # transcript exists, no live pid — injectable
    REMOTE = "remote"    # cloud-only, no local transcript — deep-link only

class Capability(StrEnum):
    TRANSCRIPT = "transcript"; INJECT = "inject"; CHAT = "chat"; DEEPLINK = "deeplink"

@dataclass(frozen=True)
class Account:
    key: str             # "claude_code:main" — provider_id ":" label-slug
    provider_id: str
    label: str           # from config: "main", "alt"
    root: Path           # CLAUDE_CONFIG_DIR for claude_code

@dataclass
class Session:
    key: str             # f"{account.key}:{session_id}" — used in all URLs (urlsafe)
    account_key: str
    session_id: str      # provider-native id (Claude UUID)
    status: SessionStatus
    cwd: Path | None
    title: str | None    # from history.jsonl "display"
    last_prompt: str | None
    model: str | None    # last assistant line's model
    kind: str | None     # "interactive" | "sdk-cli" | RC worker …
    pid: int | None
    proc_start: str | None   # /proc starttime token — pid-reuse guard
    started_at: datetime | None
    last_activity: datetime | None
    tokens: TokenTotals | None   # summed from transcript usage blocks
    deep_link: str | None        # claude.ai/code URL when applicable
    capabilities: set[Capability]

@dataclass
class UsageSnapshot:
    account_key: str
    five_hour_pct: float | None
    five_hour_resets_at: datetime | None
    seven_day_pct: float | None
    seven_day_resets_at: datetime | None
    fetched_at: datetime
    stale: bool          # true when backoff/errors mean this is old data

@dataclass
class TranscriptEvent:     # normalized transcript line
    seq: int               # monotonically increasing per session (line number)
    role: str              # "user" | "assistant" | "tool" | "system"
    text: str | None
    tool_name: str | None
    tool_summary: str | None   # short rendering of tool_use input
    model: str | None
    usage: dict | None     # raw usage block passthrough
    ts: datetime | None
    subagent: str | None   # set when from <uuid>/subagents/
```

**Provider interface** — `src/agentdeck/providers/base.py`:

```python
class SessionProvider(ABC):
    provider_id: ClassVar[str]

    @abstractmethod
    async def scan_sessions(self, account: Account) -> list[Session]: ...
    @abstractmethod
    def watch_paths(self, account: Account) -> list[Path]: ...       # for watchfiles
    @abstractmethod
    async def read_transcript(self, account, session, after_seq: int = 0) -> list[TranscriptEvent]: ...
    @abstractmethod
    async def fetch_usage(self, account: Account) -> UsageSnapshot | None: ...
    @abstractmethod
    async def inject(self, account, session, message: str) -> InjectResult: ...
    @abstractmethod
    async def open_chat(self, account, session) -> ChatHandle: ...
    # Providers without a feature return sessions whose .capabilities simply omit
    # INJECT/CHAT — the web layer keys off capabilities, never off provider type.
```

Rules that keep the abstraction honest for future Codex/Gemini providers:
- The web layer imports **only** `models.py` and `providers/__init__.py` — never `providers/claude_code/*`.
- Anything Claude-specific (config-dir slugs, credentials, `anthropic-beta` header, trust dialog) stays under `providers/claude_code/`.
- Usage limits are optional per provider (`fetch_usage` may return `None`); the limit-bar header renders per account that has data.
- Session keys are opaque strings end-to-end; routes take `{session_key}` and resolve via `AppState`.

---

## 3. Collector design

`src/agentdeck/collector.py` runs one `AccountCollector` asyncio task-group per account, started in the FastAPI lifespan.

**Hybrid watch + scan** (watchfiles alone is insufficient because pid liveness is not a filesystem event — registry files linger after process death):

1. **watchfiles.awatch** over `provider.watch_paths(account)` — for Claude: `$CFG/sessions/`, `$CFG/history.jsonl`, and `$CFG/projects/` (recursive). Debounce 300 ms. On events touching `sessions/` or `history.jsonl` → targeted rescan of session metadata. On events touching `projects/<slug>/<uuid>.jsonl` → incremental transcript tail for that session (read from stored byte offset, parse new lines, publish `transcript:<key>` events).
2. **Liveness sweep** every `liveness_interval_s` (default 10 s): for each session with a pid, check `/proc/<pid>/stat` exists AND field 22 (starttime) equals the recorded `procStart` (pid-reuse guard). Flip LIVE→IDLE on death, publish `session` event.
3. **Full rescan** every `scan_interval_s` (default 60 s) as a safety net for missed inotify events (watch-limit exhaustion, atomic renames).

Transcript reading is incremental: `transcripts.py` keeps `{session_key: (byte_offset, seq)}` in `AppState`; on change, `seek(offset)`, read complete lines only (tolerate a partial trailing line), parse, advance. Malformed JSON lines are skipped with a counter, never fatal.

**Usage-limit poller** — `providers/claude_code/usage.py`, one task per account:

- Loop: read token **fresh** from `$CFG/.credentials.json` each cycle (tokens rotate), `GET https://api.anthropic.com/api/oauth/usage` with `Authorization: Bearer …`, `anthropic-beta: oauth-2025-04-20`, and a Claude-Code-style `User-Agent`. Parse `five_hour`/`seven_day` utilization + `resets_at`.
- Interval: `usage_interval_s` per account, default 300, with ±10% jitter and per-account phase offset so two accounts never fire simultaneously.
- **429/5xx backoff**: exponential ×2 starting from the base interval, capped at 1800 s; mark snapshot `stale=True` (UI greys the bar and shows age). **401**: re-read credentials once immediately (rotation race), then back off.
- **Shared cache**: write each snapshot atomically to `$XDG_RUNTIME_DIR/agentdeck/usage-<account-label>.json` (`{fetched_at, five_hour, seven_day, resets_at}`). Config option `usage.shared_cache_dir` to relocate. This lets `kanban_poll.sh` later switch to reading agentdeck's cache — document in README. On startup, seed from this cache if fresh, so restarts don't trigger an immediate API hit.

**State + persistence**:

- `state.py`: `AppState` holds `dict[str, Session]`, `dict[str, UsageSnapshot]`, transcript offsets, chat handles. Single source of truth; mutations publish to `events.EventBus` (per-topic asyncio fanout queues with slow-consumer drop).
- `db.py`: SQLite at `~/.local/share/agentdeck/agentdeck.db` (configurable), WAL mode, stdlib `sqlite3` via `asyncio.to_thread`. v0.2 tables: `usage_history(account_key, ts, five_hour_pct, seven_day_pct)` (sparklines) and `sessions_seen(session_key, first_seen, last_seen, title, cwd)`. `history.enabled = false` disables it entirely; the app must run fully from memory.

---

## 4. Web layer

**Pages** (`routes_pages.py`):

| Route | Template | Content |
|---|---|---|
| `GET /` | `dashboard.html` | limit-bar header + session cards grouped by account, LIVE first |
| `GET /sessions/{session_key}` | `session.html` | metadata header, transcript (live-tailing when LIVE), todos, action bar (Inject / Open chat / claude.ai deep-link per capabilities) |
| `GET /sessions/{session_key}/chat` | `chat.html` | interactive stream-json chat (v0.3) |
| `GET /healthz` | JSON | liveness for systemd/uptime checks |

**Partials** (`routes_partials.py`) — every partial is also what SSE re-renders:

- `GET /partials/limit-bars` → all accounts' bars
- `GET /partials/sessions` → full session list
- `GET /partials/sessions/{session_key}/transcript?after={seq}` → events after seq (lazy backfill/pagination: newest page first, "load earlier" with `hx-get` prepend)

**SSE** (`routes_sse.py`) — two streams to avoid fanning every transcript token to dashboard viewers:

- `GET /events` (dashboard scope). Named events, each carrying **rendered HTML** (HTMX SSE style, no client JS):
  - `event: usage` → rendered `limit_bars.html` fragment
  - `event: sessions` → rendered `session_list.html` fragment (coalesced, max 1/s)
- `GET /events/sessions/{session_key}` (detail scope):
  - `event: transcript` → `transcript_event.html` fragments (appended)
  - `event: status` → session header fragment (LIVE/IDLE flips re-enable/disable the inject form)
  - `event: chat` → chat message fragments incl. partial-assistant updates (v0.3)

**HTMX patterns**:

- `base.html`: `<body hx-ext="sse" sse-connect="/events">`; header contains `<div sse-swap="usage">` with the limit bars — **always visible on every page**, sticky-top on mobile.
- Dashboard: `<div sse-swap="sessions">` around the list; initial render server-side, SSE replaces wholesale (list is small; no per-card diffing in v1).
- Session detail: transcript container `sse-connect="/events/sessions/{key}"` with `sse-swap="transcript" hx-swap="beforeend"`, plus CSS pin-to-bottom.
- Inject form: `<form hx-post="/sessions/{key}/inject" hx-target="#inject-result" hx-disabled-elt="this">`.
- Limit bar component (`partials/limit_bars.html`): per account a labeled pair of progress bars (5h / 7d) with pct, `resets_at` as server-rendered relative time ("resets in 2h 10m"), color thresholds at 70/90%, greyed + "(stale, 12m)" when `snapshot.stale`.

**Actions** (`routes_actions.py`):

- `POST /sessions/{session_key}/inject` (form field `message`) → interlocks + one-shot resume; returns result fragment (success + response summary, or refusal reason)
- `POST /sessions/{session_key}/chat/open`, `POST .../chat/send`, `POST .../chat/stop` (v0.3)

**Auth headroom**: all routers mounted through a single `Depends(require_access)` dependency in `web/deps.py` — a no-op in v1. Adding token/pass auth later = implementing that one function + a login page; no route changes. Bind address from config; default `127.0.0.1` so an unconfigured install exposes nothing.

---

## 5. Message-inject subsystem

`providers/claude_code/inject.py`, orchestrated by an `InjectorService` on `AppState` holding a per-session `asyncio.Lock` and a registry of owned child pids.

**Safety interlocks — checked in order, every attempt, immediately before spawn**:

1. **Liveness**: re-scan `$CFG/sessions/*.json` + `/proc` *at inject time* (not from cached state). If any live pid owns this sessionId → refuse: "session is live; use the claude.ai deep-link" (single-writer JSONL rule). A pid in our own owned-children registry is ours — still refuse a *second* concurrent writer via the per-session lock.
2. **cwd exists** and is a directory (session may reference a deleted worktree).
3. **Trust dialog**: read `$CFG/.claude.json` (fall back to `~/.claude.json` for the default dir) and require `projects[cwd].hasTrustDialogAccepted == true`. If not → refuse with remediation text ("run `claude` once in this directory to accept trust"). **Never auto-write that file** — silently trusting directories is a security decision the user must make.
4. Config kill-switch: `[inject] enabled = false` hides the forms and 403s the routes.

**One-shot inject** (idle sessions):

```
env = {**os.environ, "CLAUDE_CONFIG_DIR": str(account.root)}
claude -p --resume <uuid> "<msg>"    # cwd=session.cwd, stdin=DEVNULL
```

`asyncio.create_subprocess_exec`, capture stdout/stderr, hard timeout `inject.timeout_s` (default 600; kill process group on expiry). We do NOT pass `--fork-session`, so the reply lands in the same JSONL and the collector's transcript tail picks it up naturally; the inject-result fragment only needs exit status + first lines of stdout.

**Long-lived chat** (v0.3): `ChatSession` object per session:

```
claude -p --resume <id> --input-format stream-json --output-format stream-json \
       --include-partial-messages
```

- stdin writer task (JSON user messages), stdout reader task (line-parse → normalize → publish `chat` SSE events; partial messages update the in-flight assistant bubble via `hx-swap-oob` fragment re-render).
- Lifecycle: created on `POST /chat/open`; idle timeout `chat_idle_timeout_s` (default 600 s) tears it down; `POST /chat/stop` and app shutdown (lifespan finally-block) send SIGTERM → 5 s → SIGKILL to the process group. `KillMode=control-group` in the unit is the backstop.
- One chat per session (lock); one-shot inject refused while a chat is open.
- The chat child appears in `sessions/<pid>.json` — the registry scanner must tag agentdeck-owned pids (match against `InjectorService.owned_pids`) so the dashboard shows "chatting via agentdeck", not a false LIVE lockout.

---

## 6. Config file format

`~/.config/agentdeck/config.toml` (path overridable via `AGENTDECK_CONFIG`); `config.example.toml` committed:

```toml
[server]
bind = "127.0.0.1"        # set to your Tailscale IP, e.g. "100.101.102.103"
port = 8756

[polling]
usage_interval_s = 300
scan_interval_s = 60
liveness_interval_s = 10

[usage]
shared_cache_dir = ""      # default: $XDG_RUNTIME_DIR/agentdeck

[history]
enabled = true
db_path = "~/.local/share/agentdeck/agentdeck.db"
usage_retention_days = 30

[inject]
enabled = true
timeout_s = 600
chat_idle_timeout_s = 600

[[accounts]]
provider = "claude_code"
label = "main"
config_dir = "~/.claude"

[[accounts]]
provider = "claude_code"
label = "alt"
config_dir = "~/.claude2"
```

Loaded via `pydantic-settings` models in `config.py`; `provider` values validated against the `PROVIDERS` registry; labels unique and slug-safe (they appear in URLs and cache filenames). The config file contains **no secrets** — credentials always come from the provider's own store.

---

## 7. Phased milestones

**v0.1 — read-only dashboard (keep it genuinely small)**
1. Repo scaffold: pyproject, LICENSE, .gitignore, config.example.toml, systemd unit, README skeleton.
2. `config.py`, `models.py`, `state.py`, `events.py`.
3. `claude_code/registry.py` (+ liveness), `claude_code/history.py` (titles/last-prompt), `claude_code/provider.py::scan_sessions`. No transcript parsing yet beyond `last_activity` from file mtime.
4. `claude_code/credentials.py` + `usage.py` poller with backoff + shared cache.
5. `collector.py` with the scan/liveness/watch loops (watchfiles may be deferred to a pure 15 s scan in v0.1 — the interface doesn't change).
6. Web: `GET /`, `GET /healthz`, `/partials/limit-bars`, `/partials/sessions`, `GET /events` (usage + sessions), `base.html` + `dashboard.html` + limit bars, deep-links for LIVE/REMOTE sessions.
7. Deploy on the host as the systemd user unit; first public push.
   **Definition of done**: phone on Tailscale shows both accounts' limit bars updating and an accurate live/idle session list; zero write paths exist.

**v0.2 — transcript viewer**
1. `claude_code/transcripts.py` incremental parser (usage blocks → token totals; tool_use summarization; subagent transcripts collapsed inline; todos from `tasks/<sessionId>/`).
2. `GET /sessions/{key}` + transcript partial with "load earlier" pagination; per-session SSE stream with live tail for LIVE sessions.
3. SQLite `db.py`: usage history + sessions_seen; tiny usage sparkline in the header.

**v0.3 — message injection / chat**
1. `inject.py` one-shot resume + interlocks; `POST /sessions/{key}/inject`; result fragment; docs on the trust-dialog remediation.
2. `ChatSession` stream-json child + `chat.html` + chat SSE; lifecycle + owned-pid tagging.
3. Manual soak on the host (inject into a scratch project, verify no live-session corruption possible).

**v0.4 — provider abstraction cleanup + docs**
1. Audit: no `claude_code` imports outside `providers/`; freeze `base.py`; write `docs/providers.md` with a worked "what a Codex CLI provider would implement" sketch.
2. `docs/claude-code-internals.md` (every undocumented surface + observed schema + version tested).
3. Test/CI polish (GitHub Actions: ruff + pytest), CHANGELOG, README screenshots, tag `v0.4.0`.

---

## 8. Testing approach

- **No live API, no real config dirs, ever.** `conftest.py` provides a `fake_config_dir(tmp_path)` builder that assembles a synthetic `CLAUDE_CONFIG_DIR` (sessions/, projects/, history.jsonl, tasks/, dummy `.credentials.json`) from templates in `tests/fixtures/claude_config_a/`. Fixture jsonl is hand-written matching the observed schema (including a `usage` block with `cache_creation`/`iterations` fields, a tool_use line, a malformed line, a truncated trailing line).
- **Liveness**: `registry.py` takes an injectable `proc_root: Path = Path("/proc")`; tests point it at a fixture tree with fake `<pid>/stat` files simulating live/dead/pid-reused.
- **Usage poller**: `respx` mocks the OAuth endpoint — happy path, 429→backoff schedule (assert intervals via a fake clock), 401→credential re-read, malformed JSON. Assert the token is read from disk on *every* cycle.
- **Inject**: PATH-prepend a stub `claude` script (created in `tmp_path`) that records argv/env/cwd and emits canned output; assert `CLAUDE_CONFIG_DIR`, cwd, and refusal paths (live pid, missing trust, disabled config) *without* the stub ever being spawned for refusals. Stream-json chat tested against a stub replaying a canned event stream.
- **Web**: `httpx.ASGITransport` client against the app with a pre-seeded `AppState`; assert pages render, partials contain expected session keys, SSE stream yields a `usage` event after publishing to the bus (read with a timeout).
- **Collector**: integration-style test on `tmp_path`: append a line to a fixture jsonl, assert a `transcript` event is published within the scan interval (intervals set to 0.05 s).
- Markers: everything runs offline in CI; a separate `@pytest.mark.host` suite (excluded by default) may touch the real `~/.claude` read-only for local smoke.

---

## 9. Risks / gotchas

- **Undocumented API drift**: the OAuth usage endpoint, `sessions/<pid>.json` schema, jsonl format, and `--input-format stream-json` are unversioned internals of Claude Code (v2.1.198 observed; registry files show `version` + `peerProtocol` fields — record and log them). Mitigation: every parser is total (skip-and-count on unknown shapes), `docs/claude-code-internals.md` records exact observed schemas per CLI version, and the UI shows "parser skipped N lines" rather than crashing.
- **Token handling**: `.credentials.json` is mode 600 and rotates. Read fresh per poll, hold in a local variable only, never in `AppState`, never in logs (logging filter redacting `Bearer` and `sk-ant`), never in any HTTP response. agentdeck runs as the same user; running as a different user is unsupported.
- **Public-repo hygiene**: the repo lives on the same machine as real transcripts; the biggest leak vector is a fixture "borrowed" from `~/.claude/projects/`. Mitigations: fixtures dir README ban, .gitignore patterns for `history.jsonl`/`sessions/`/`projects/` anywhere in the tree, CI grep for `sk-ant`/real-looking UUID patterns. Never commit `config.toml`.
- **Single-writer JSONL**: resuming a live session corrupts it. Interlock #1 re-checks at spawn time; inherent TOCTOU window if the user starts a TUI in the same second — acceptable residual risk; document it. Never bypass the check for "stuck" registry files; the procStart guard handles pid reuse.
- **Trust dialog**: `-p --resume` in an untrusted cwd hangs/fails silently. Refuse pre-flight rather than auto-trust.
- **Shared usage probing**: `kanban_poll.sh` spends a `claude -p /usage` call every ≤150 s against the pinned account. agentdeck's HTTP polling is independent, but publish the shared cache from day one and note in README that the kanban script can migrate to it — one poller for the whole host.
- **Bazzite/rootless specifics**: `systemd --user` needs linger for headless boot; `/var/home` vs `/home` path duality (always resolve `Path.home()`, never hardcode); `$XDG_RUNTIME_DIR` disappears if linger is off — fall back to `~/.cache/agentdeck` for the shared cache.
- **inotify limits**: recursive watch over `projects/` (hundreds of slug dirs) can exhaust `fs.inotify.max_user_watches`; the periodic full rescan is the mandated fallback and the app must keep working if the watcher task dies (log + degrade to scan-only).
- **SSE + phones**: mobile browsers drop SSE on background; rely on HTMX auto-reconnect and make every SSE fragment idempotent (full-fragment replace, plus `after=seq` backfill on transcript reconnect).
- **No auth in v1**: inject routes are remote code execution for anyone on the Tailscale network. Default bind `127.0.0.1`, loud README warning, `[inject] enabled` kill-switch, and the `require_access` dependency stub so auth can land without refactoring.
