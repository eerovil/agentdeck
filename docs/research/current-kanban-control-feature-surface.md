# Current kanban-control feature surface

Research snapshot: 2026-07-23. This inventories the checked-out Outdoor, Storm, and AgentDeck
implementations for Wayfinder issue #106. “Full parity” below means preserving the intentional
control and release semantics, not preserving current defects or turning host/product accidents into
shared invariants.

## Executive conclusion

Outdoor and Storm already share one conceptual Poller Engine: their 3,417-line
`kanban_board.py` files are byte-identical, while config, launchers, installed skill names, worker
playbooks, deployment policy, and artifacts remain project-owned. Storm explicitly documents that
copy-without-runtime-dependency arrangement ([Storm kanban-control.md:3](/var/home/eero/storm/.claude/codebase/kanban-control.md:3)). The reusable boundary should therefore be:

- **Poller Engine:** exact command intake, authorization decisions, durable command queue,
  ownership-event provenance, generation leases, dispatch receipts, lifecycle reconciliation,
  safe stop, and guarded release state machine.
- **Poller Manifest:** repositories, board/status mapping, controller policy, limits, merge policy,
  and enabled extension policy. It must remain portable and contain no secrets or machine paths, as
  AgentDeck's domain language requires ([CONTEXT.md:197](/var/home/eero/agentdeck/.worktrees/research-current-kanban-control-feature-surface/CONTEXT.md:197),
  [CONTEXT.md:203](/var/home/eero/agentdeck/.worktrees/research-current-kanban-control-feature-surface/CONTEXT.md:203)).
- **Host Overlay:** checkout/state/log paths, provider account, credentials, binaries, sockets,
  services, containers, and other machine bindings ([CONTEXT.md:208](/var/home/eero/agentdeck/.worktrees/research-current-kanban-control-feature-surface/CONTEXT.md:208)).
- **Poller Extensions:** CI diagnosis, production deployment observation, Sentry policy,
  screenshots/artifacts, and repository-specific worker/test procedures. Extensions must not replace
  authorization, queue, ownership, or lease semantics ([CONTEXT.md:235](/var/home/eero/agentdeck/.worktrees/research-current-kanban-control-feature-surface/CONTEXT.md:235),
  [CONTEXT.md:240](/var/home/eero/agentdeck/.worktrees/research-current-kanban-control-feature-surface/CONTEXT.md:240)).

The current AgentDeck `kanban/` implementation is not yet a full-parity Reference Poller Project.
It is a useful single-repo issue-to-PR/deploy prototype: it selects allow-listed authors' `agent`
issues, makes a worktree, starts a detached Codex delegation, and treats an open PR as completion
([AgentDeck poller.py:87](/var/home/eero/agentdeck/.worktrees/research-current-kanban-control-feature-surface/kanban/poller.py:87),
[AgentDeck poller.py:489](/var/home/eero/agentdeck/.worktrees/research-current-kanban-control-feature-surface/kanban/poller.py:489)). It has none of the Outdoor/Storm command queue, Project lifecycle, controller/release authorization, leases, stop protocol, merge-review protocol, Sentry intake, or linked multi-repo release behavior.

## Shared engine behavior that full parity requires

### Commands, ownership, and lifecycle

- Intake recognizes exact `/claude <instruction>`, `/claude stop`, `/merge`, and `/merge review`.
  Bot-marked comments cannot self-trigger; malformed near-matches are rejected. The grammar and
  persistent `claude` label are currently constants rather than configuration
  ([Outdoor kanban_board.py:113](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:113),
  [Outdoor kanban_board.py:446](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:446)).
- The poll scans each configured repository's paginated issue-comment feed. GitHub comment ID is
  exact identity, creation time plus ID is FIFO order, and intake is saved before dispatch. A scan or
  permission failure prevents cursor advancement, while reaction failures are cosmetic
  ([Outdoor kanban_board.py:577](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:577),
  [Outdoor kanban_board.py:647](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:647)).
- Project V2 `Status` is the external lifecycle source. The engine requires Todo, In progress, In
  review, Done, Needs More Info, and Blocked; Backlog is optional. Display spelling is normalized
  case-insensitively, which Storm needs for `In Progress` versus engine `In progress`
  ([Outdoor kanban_board.py:503](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:503),
  [Storm test_kanban_board.py:59](/var/home/eero/storm/Docker/claude-skills/kanban-poller-storm/test_kanban_board.py:59)).
- `claude` means persistent ownership, not current activity. Manual add starts or restarts; removal is
  a safe stop. Ownership-event IDs are tracked independently so state recovery should not reinterpret
  an old persistent label as a fresh request ([Outdoor kanban_board.py:694](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:694),
  [Outdoor kanban_board.py:725](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:725)).
- One card has at most one worker. Instructions received while active append to `pending`; on the next
  turn the complete ordered batch moves to `processing` and produces one `followup` directive.
  Same-scan instructions/stops are applied before merge commands, so new work always blocks an
  irreversible release ([Outdoor kanban_board.py:1217](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:1217),
  [Outdoor kanban_board.py:1754](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:1754)).

### Authorization and trust

- A controller is accepted when GitHub says OWNER/MEMBER/COLLABORATOR or a live collaborator lookup
  returns triage/write/maintain/admin. A transient or malformed permission response is retried, not
  treated as denial ([Outdoor kanban_board.py:541](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:541)).
- Current code applies that same authorization to work commands and `/merge`; there is no more
  privileged release-controller check because authorization happens before command-specific handling
  ([Outdoor kanban_board.py:615](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:615)). This is current behavior, not a sound generic invariant. The Manifest should be able to make Release Controllers stricter than Work Controllers, matching AgentDeck's domain distinction
  ([CONTEXT.md:213](/var/home/eero/agentdeck/.worktrees/research-current-kanban-control-feature-surface/CONTEXT.md:213),
  [CONTEXT.md:218](/var/home/eero/agentdeck/.worktrees/research-current-kanban-control-feature-surface/CONTEXT.md:218)).
- Board writes and comments use `GH_TOKEN` loaded from environment or configured env files. Protected
  merge/close operations deliberately unset it and use the host `gh` login because the kanban PAT is
  underprivileged for protected `master` ([Outdoor kanban_board.py:178](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:178),
  [Outdoor kanban_board.py:2305](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:2305)). A reusable engine needs named credential capabilities, not implicit “PAT versus this operator's host login.”

### Queue, lease, dispatch, stop, and recovery

- Every normal dispatch has a UUID `generation` lease and a separate `dispatch_id`. Project status,
  queue/lease, and send intent are persisted before emitting the directive. The worker must present
  the exact generation before comments, edits, pushes, and terminal results
  ([Outdoor kanban_board.py:1453](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:1453),
  [Outdoor dispatcher SKILL.md:90](/var/home/eero/outdoor/Docker/claude-skills/kanban-dispatch/SKILL.md:90)).
- RemoteTrigger HTTP 200 is not enough: the dispatcher writes `dispatch-ack` for the exact durable
  attempt. Missing acknowledgement retries after 90 seconds, up to three attempts, with a new
  generation that disables an ambiguously spawned predecessor
  ([Outdoor kanban_board.py:1496](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:1496),
  [Outdoor dispatcher SKILL.md:105](/var/home/eero/outdoor/Docker/claude-skills/kanban-dispatch/SKILL.md:105)).
- Completion is positive and lease-bound: `finish` posts a hidden terminal marker containing the
  generation and target status before writing Project status. Reconcile ignores a human status move
  without the current marker and can finish a partial status-write failure from the marker
  ([Outdoor kanban_board.py:1628](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:1628),
  [Storm test_kanban_board.py:130](/var/home/eero/storm/Docker/claude-skills/kanban-poller-storm/test_kanban_board.py:130)).
- Stop cancels pending work, invalidates the current generation before label side effects, and waits
  for `stop-ack` at a worker safe boundary. Dead workers release after the two-hour stale timeout;
  a later instruction can resume the existing branch/PR/worktree under a new lease
  ([Outdoor kanban_board.py:1050](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:1050),
  [Outdoor kanban_board.py:1688](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:1688)).
- Stale active work cold-reloads through the same capped, acknowledged dispatch path. The global
  worker cap counts active workers and reservations; the token-budget guard holds queued work but
  continues deterministic merge/deploy reconciliation
  ([Outdoor kanban_board.py:1730](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:1730),
  [Outdoor kanban_board.py:1818](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:1818)).

### Durable state model

The Poller Engine currently owns several coordinated durable surfaces, not one dispatch timestamp:

| Surface | Required content / role |
| --- | --- |
| `state.json` + `.bak` | Per-card status, kind, repositories, branch(es), worktrees, session context, pending/processing command IDs, generation, dispatch attempt, stop/release state, merge/review/deploy state, budget notices, and Sentry state. |
| `controls.json` + `.bak` | Independent accepted/rejected/queued/processing/done command ledger, scan cursor, and enough issue context to reconstruct lost worker state. |
| `ownership.json` | Last observed ownership-label event and migration baselines. |
| `stop-acks/`, `dispatch-acks/` | Exact generation/attempt receipts crossing poller and worker/dispatcher processes. |
| `state.json.lock` | Whole read-modify-write transaction serialization across poll and worker commands. |
| GitHub | External source of truth for open/closed, label ownership, Project status, reactions, PR heads/checks/merge, and terminal/review markers in comments. |

The files and paths are derived together from `state_path`
([Outdoor kanban_board.py:90](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:90)). Writes use fsync plus atomic replacement; state and controls keep last-valid backups
([Outdoor kanban_board.py:2033](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:2033),
[Outdoor kanban_board.py:2046](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:2046)). The independent controls ledger is published before worker state and reconstructs queued/processing commands after deletion or rollback
([Outdoor kanban_board.py:2101](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:2101),
[Outdoor kanban_board.py:2179](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:2179)).

A reusable engine needs an explicit versioned state schema and migrations for all of these concepts.
It must also preserve the distinction between poller state, GitHub external truth, and worker-session
lineage; they cannot be collapsed into a single “working” record.

## Guarded merge, release, and deploy

- `/merge` and `/merge review` require open + owned + exact In review + no active/pending work + a
  complete linked PR set. PR discovery recognizes a PR card, GitHub's native same-repo closing
  references, and explicit cross-repo references; any failed listing or malformed response defers the
  entire release ([Outdoor kanban_board.py:1141](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:1141),
  [Outdoor kanban_board.py:2498](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:2498)).
- `/merge review` snapshots every open linked PR head, dispatches one leased high-effort reviewer,
  and enters release only on `pass` with unchanged PR set/heads and no queued instruction. Any push
  or unresolved finding returns to In review ([Outdoor kanban_board.py:1188](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:1188),
  [Outdoor kanban_board.py:1341](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:1341)).
- The poll, not the worker, classifies checks, updates behind branches, allows one bounded
  `merge-fix` attempt for red/conflicting PRs, arms GitHub native auto-merge, and observes the result.
  Code/conflict fixes return for human review; lint/update-only fixes may continue
  ([Outdoor kanban_board.py:2613](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:2613),
  [Outdoor kanban_board.py:2721](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:2721)).
- Completion and failure are multi-wake retryable transitions. Done writes status, posts summary,
  verifies close, removes ownership, resolves the command, cleans worktrees, and only then deletes
  state; a release/deploy failure reopens if necessary and lands in Blocked with ownership retained
  ([Outdoor kanban_board.py:2873](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:2873),
  [Outdoor kanban_board.py:2917](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:2917)).

The shared state machine has project policy at its seams:

| Project | Release policy |
| --- | --- |
| Outdoor | GitHub merge commit; `store` and `tilhi` then wait for the matching Drone `promote` build with `deploy_to=production`, not the earlier push build ([Outdoor config.json:27](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban.config.json:27), [Outdoor kanban_board.py:2406](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:2406)). |
| Storm | GitHub squash; no production deploy gate because `auto_deploy_repos` is empty ([Storm config.json:23](/var/home/eero/storm/Docker/claude-skills/kanban-poller-storm/kanban.config.json:23), [Storm kanban-control.md:105](/var/home/eero/storm/.claude/codebase/kanban-control.md:105)). |

Drone's REST model, `production` promotion target, and special step names `Check_up_to_date` and
`Check_failure` are Outdoor extension knowledge, not generic CI semantics
([Outdoor kanban_board.py:2355](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:2355),
[Outdoor kanban_board.py:2426](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:2426)).

## Sentry and artifacts are extensions

Both projects enable Sentry capability, but automatic ingestion additionally requires
`KANBAN_AUTOFIX`. First activation anchors a high-water at “now”; later unresolved, unassigned,
non-staging/non-localdev groups enter an impact-ranked backlog. Promotion is at most one issue per
wake and is bounded by global worker cap, autofix worker cap, daily cap, and token budget
([Outdoor kanban_board.py:160](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:160),
[Outdoor kanban_board.py:3188](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:3188),
[Outdoor kanban_board.py:3254](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:3254)). Manual `sentry-add` bypasses automatic eligibility/high-water, but not promotion safety caps
([Outdoor kanban_board.py:3308](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:3308)).

- Outdoor maps seven Sentry projects to four code repos and gives `tilhi-javascript` a special
  error-only three-distinct-traces/15-minute gate, five-minute rechecks, seven-day watch, and re-arm
  behavior ([Outdoor config.json:29](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban.config.json:29),
  [Outdoor kanban_board.py:2998](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:2998)).
- Storm maps only `storm-e3-prod`; with no `browser_project`, the Tilhi-specific watch is inert
  ([Storm config.json:25](/var/home/eero/storm/Docker/claude-skills/kanban-poller-storm/kanban.config.json:25),
  [Storm kanban-control.md:138](/var/home/eero/storm/.claude/codebase/kanban-control.md:138)).
- Both worker playbooks self-triage autofix issues before creating a worktree into FIX, CAN'T-FIX,
  or PARK. The latter outcomes remain open, Blocked, and owned for deliberate later resumption
  ([Storm worker SKILL.md:126](/var/home/eero/storm/Docker/claude-skills/kanban-worker-storm/SKILL.md:126)).

Artifacts diverge further. Outdoor's worker defaults to visual/artifact evidence and its 534-line
`kanban_shot.py` knows the store/Tilhi Podman stack, bind-mounted worktrees, local TLS/vhosts,
admin/customer login, Playwright interactions, and release-asset hosting
([Outdoor worker SKILL.md:210](/var/home/eero/outdoor/Docker/claude-skills/kanban-worker/SKILL.md:210),
[Outdoor kanban_shot.py:60](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_shot.py:60)). Storm has no equivalent harness; screenshots are merely a possible natural-language worker request
([Storm worker SKILL.md:176](/var/home/eero/storm/Docker/claude-skills/kanban-worker-storm/SKILL.md:176)). AgentDeck has a third, repository-specific best-effort ASGI screenshot uploader
([AgentDeck shot.py:2](/var/home/eero/agentdeck/.worktrees/research-current-kanban-control-feature-surface/kanban/shot.py:2)). These belong behind an artifact extension contract, not in the engine.

## Worker and dispatcher contracts

The current backend is Claude Remote Control, not AgentDeck:

1. A 30-second timer invokes a flocked shell launcher. It pins a Claude profile, probes `/usage`,
   runs the token-free Python poll, and invokes headless `claude -p` only when JSON directives exist
   ([Outdoor launcher:25](/var/home/eero/outdoor/Docker/bin/kanban_poll.sh:25),
   [Outdoor launcher:75](/var/home/eero/outdoor/Docker/bin/kanban_poll.sh:75),
   [Outdoor launcher:129](/var/home/eero/outdoor/Docker/bin/kanban_poll.sh:129)).
2. The engine resolves the current rotating bridge environment ID from the active
   `CLAUDE_CONFIG_DIR` pointer and stamps each directive
   ([Outdoor kanban_board.py:2209](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:2209),
   [Outdoor kanban_board.py:3371](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:3371)).
3. A thin namespaced dispatcher performs exactly one persistent RemoteTrigger creation per
   directive. Actions are `new`, `pr`, `resume`, `followup`, `merge-review`, and `merge-fix`; queued
   message payloads and generation are copied verbatim into the worker prompt
   ([Outdoor dispatcher SKILL.md:35](/var/home/eero/outdoor/Docker/claude-skills/kanban-dispatch/SKILL.md:35),
   [Outdoor dispatcher SKILL.md:68](/var/home/eero/outdoor/Docker/claude-skills/kanban-dispatch/SKILL.md:68)).
4. One worker session owns one card. Outdoor may touch several code repos; Storm is one repo. Workers
   create/reuse worktrees, test in real containers, push/open or update PRs, and report only through
   lease-aware helper commands. They never merge, deploy, or change ownership directly
   ([Outdoor worker SKILL.md:6](/var/home/eero/outdoor/Docker/claude-skills/kanban-worker/SKILL.md:6),
   [Storm worker SKILL.md:28](/var/home/eero/storm/Docker/claude-skills/kanban-worker-storm/SKILL.md:28)).

AgentDeck already exposes the better reusable execution primitive: opaque key + stable
`delivery_id` + message, with at-most-one live worker, persisted receipts, steering/queueing of a
live worker, revival of known lineage, and fresh spawn when unknown
([WORKER_RUNTIME.md:24](/var/home/eero/agentdeck/.worktrees/research-current-kanban-control-feature-surface/docs/WORKER_RUNTIME.md:24)). Its web API proxies deliver/interrupt/stop/park/release/forget to the Persistent Runtime
([routes_api.py:91](/var/home/eero/agentdeck/.worktrees/research-current-kanban-control-feature-surface/src/agentdeck/web/routes_api.py:91)). The Poller Engine should use that as a worker-backend port while retaining poller-owned GitHub lifecycle and queues. Stable delivery IDs must survive retries; an `uncertain` accepted result explicitly forbids resending the same logical delivery
([Claude worker.py:431](/var/home/eero/agentdeck/.worktrees/research-current-kanban-control-feature-surface/src/agentdeck/providers/claude_code/worker.py:431)).

By contrast, the current Reference Poller Project starts a detached `agentdeck delegate` Codex CLI
and marks the card `working` immediately after `Popen`, without an acceptance receipt or delivery ID
([AgentDeck poller.py:463](/var/home/eero/agentdeck/.worktrees/research-current-kanban-control-feature-surface/kanban/poller.py:463),
[AgentDeck poller.py:543](/var/home/eero/agentdeck/.worktrees/research-current-kanban-control-feature-surface/kanban/poller.py:543)). Its two-hour stale retry is therefore not equivalent to the current 90-second acknowledged lease protocol.

## Configuration and operational inventory

The current adjacent JSON mixes all future configuration classes. The shared loader requires org,
Project number/node ID, status field/option IDs, repository-to-path map, and state path; it defaults
worker cap 10, staleness two hours, budget cutoff 90%, merge timeout 45 minutes, and merge method
`merge` ([Outdoor kanban_board.py:25](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:25),
[Outdoor kanban_board.py:42](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:42),
[Outdoor kanban_board.py:53](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:53)).

| Class | Current values that must be represented |
| --- | --- |
| Manifest / activation-resolved | GitHub org; Project and Status IDs; issue repos versus code repos; status names/options; ownership label and command grammar; controller/release policy; global/stale/budget/dispatch/merge timeouts; merge method; auto-deploy repos; Sentry projects/mappings/caps/browser policy. Outdoor's concrete multi-repo/board values are visible in its config ([Outdoor config.json:2](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban.config.json:2)); Storm's single-repo values differ ([Storm config.json:3](/var/home/eero/storm/Docker/claude-skills/kanban-poller-storm/kanban.config.json:3)). |
| Host Overlay | Checkout and worktree roots; state/backup/ack paths; env files; log/lock/usage-cache paths; Claude/AgentDeck binary and account; bridge pointer or runtime URL/socket; systemd unit names; container names and mount paths; Drone/Sentry/GitHub credential bindings. |
| Extension configuration | CI/check adapter and failure classifier; production deploy observer; Sentry ingress; artifact uploader; per-repo branch/test/build/migration/worktree procedure; localization/comment templates. |
| Activation checks | Board field/options and labels exist; repositories are accessible; controller and merge credentials have distinct required capabilities; native auto-merge/branch protection are compatible; worker backend/account and budget probe are live; state directory is writable/lockable; services/timer and installed worker assets match the selected version. |

Operational dependencies are substantial: Python 3 and stdlib, `gh`, GitHub REST/GraphQL/Projects
V2, network, host credential store plus PAT, `flock`, systemd user timers, Claude CLI subscription and
RemoteTrigger connector, rotating RC bridge pointer, installed skills, Git, and project containers.
Outdoor additionally has Drone, Sentry, Podman/Playwright/nginx/local certificates, and a GitHub
asset repository. Skills are copied, not linked, into each selected Claude profile, so source and
installed versions can drift ([Outdoor installer:21](/var/home/eero/outdoor/Docker/install-claude-skills.sh:21),
[Storm installer:19](/var/home/eero/storm/.devcontainer/install-claude-skills.sh:19)). Storm namespaces its skills, units, state/log/lock, and owns a dedicated RC service
([Storm launcher:10](/var/home/eero/storm/Docker/bin/kanban_poll.sh:10),
[claude-rc-storm.service:7](/var/home/eero/storm/Docker/systemd/claude-rc-storm.service:7)). Outdoor's launcher additionally enforces a host-global persisted-session process cap that can reap an idle interactive chat, an accepted host-memory tradeoff rather than Poller Engine policy
([Outdoor launcher:45](/var/home/eero/outdoor/Docker/bin/kanban_poll.sh:45)).

Observability is currently human-oriented: GitHub reactions/comments/status, jump-in URLs,
`todo`/`issue`/`state-get`/`reconcile` commands, JSON files, systemd timer state, and append-only `/tmp`
logs. The timer units run every 30 seconds and intentionally do not replay missed ticks
([Outdoor timer:4](/var/home/eero/outdoor/Docker/systemd/kanban-poll.timer:4)). A reusable instance needs structured health/counters for scans, cursor age, queue depth, dispatch attempts, worker/backend availability, state fallback, budget holds, releases, deployments, and extension failures in addition to these operator surfaces.

## Security assumptions that must stay explicit

- GitHub comment bodies and issue content become autonomous worker instructions. Controller policy,
  worker permissions/tools, checkout roots, and allowed repositories are an RCE boundary.
- The current RemoteTrigger worker is intentionally persistent and receives a broad allowed-tools
  list ([Outdoor dispatcher SKILL.md:42](/var/home/eero/outdoor/Docker/claude-skills/kanban-dispatch/SKILL.md:42)). Its bridge environment must already trust the workspace and possess the correct GitHub/container credentials.
- Storm must strip `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` so `/usage` and RemoteTrigger use the
  subscription connector; Outdoor and Storm account selection otherwise depends on
  `CLAUDE_CONFIG_DIR` ([Storm launcher:41](/var/home/eero/storm/Docker/bin/kanban_poll.sh:41),
  [Storm launcher:114](/var/home/eero/storm/Docker/bin/kanban_poll.sh:114)). This is backend-adapter/overlay behavior.
- AgentDeck's `/api` access dependency is currently a no-op, including worker and delegation routes;
  safety relies on localhost or a trusted private bind ([deps.py:19](/var/home/eero/agentdeck/.worktrees/research-current-kanban-control-feature-surface/src/agentdeck/web/deps.py:19)). Poller activation must validate that assumption or add real machine authentication before treating the API as a remotely reachable engine/worker boundary.
- The current AgentDeck prototype's issue-author allowlist fails open when empty
  ([AgentDeck poller.py:87](/var/home/eero/agentdeck/.worktrees/research-current-kanban-control-feature-surface/kanban/poller.py:87),
  [AgentDeck test_kanban_poller.py:50](/var/home/eero/agentdeck/.worktrees/research-current-kanban-control-feature-surface/tests/test_kanban_poller.py:50)). A public Reference Poller Manifest should reject an empty allowlist unless accepting every author is an explicit activation decision.

## Hardcoded knowledge that must not become generic invariants

- Finnish footer/rejection/budget/release text; the words `claude`, `/claude`, and `/merge`; the exact
  lifecycle names; and GitHub's field name `Status` should be workflow schema/localization policy,
  even if the Reference Poller chooses these defaults.
- `master`, `claude/issue-N-*`, GitHub closing keywords, merge/squash choice, issue-repo versus
  code-repo linkage, and Outdoor's `sos` checkout → `store` repository mapping are project VCS policy.
- Drone event/step names, promotion target, branch-protection behavior, and “store/Tilhi deploys,
  Storm does not” are release extensions.
- `tilhi-javascript`, production environment exclusions, Sentry org/project mappings, trace/replay
  identity, and FIX/CAN'T-FIX/PARK prompts are incident extension policy.
- `/var/home/eero/...`, `~/.claude*`, `.worktrees/issue-N`, `/code`, container/venv/service names,
  local TLS/vhosts, shared databases, and exact test/build commands are Host Overlay or project worker
  assets.
- Claude Remote bridge-pointer shape, `cse_*` process argv, session reaping, RemoteTrigger payload/tool
  names, and the broad worker tool list are one worker-backend implementation, not Poller Engine API.
- AgentDeck's current `kanban/` choices—single repository, `agent` label, allow-listed issue authors,
  local Codex delegation, own `.venv`, `kanban-shots` release, and direct live-checkout deployment—are
  the future Reference Poller Project's extensions and overlay, not reusable engine behavior
  ([AgentDeck config.json:2](/var/home/eero/agentdeck/.worktrees/research-current-kanban-control-feature-surface/kanban/config.json:2),
  [AgentDeck README.md:31](/var/home/eero/agentdeck/.worktrees/research-current-kanban-control-feature-surface/kanban/README.md:31)).

## Known correctness and operability gaps to avoid preserving

1. **Poller-owned label additions can look manual.** `_handle_claude_control` adds `claude` but does
   not advance/suppress the ownership-event ledger ([Outdoor kanban_board.py:1023](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:1023)). On the next wake, `_manual_label_starts` classifies any changed latest event as a manual start
   ([Outdoor kanban_board.py:776](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:776)). Internal ownership mutations need durable provenance in the same transaction/state machine.
2. **Worktree cleanup lacks durable registration and retry.** The worker creates and relativizes
   worktrees but never calls `state-set` to record `worktrees`
   ([Outdoor worker SKILL.md:118](/var/home/eero/outdoor/Docker/claude-skills/kanban-worker/SKILL.md:118)); cleanup iterates only `rec.get("worktrees", [])` and ignores the `git worktree remove` result
   ([Outdoor kanban_board.py:2274](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:2274)). Registration must be part of the worktree-create contract, and failed cleanup must retain retryable state.
3. **Not every spawn uses the acknowledged-dispatch protocol.** `merge-fix` intentionally has no
   `dispatch_id` ([Outdoor kanban_board.py:2761](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:2761)); initial Sentry promotion also emits a `new` directive with only a generation
   ([Outdoor kanban_board.py:3290](/var/home/eero/outdoor/Docker/claude-skills/kanban-poller/kanban_board.py:3290)), despite dispatcher documentation calling merge-fix the sole legacy exception. Both should use the same durable delivery/receipt abstraction.
4. **Systemd cannot reliably alert on poll/dispatch failure.** The launcher logs a nonzero poll result
   and exits 0, then logs the dispatcher result without propagating it
   ([Outdoor launcher:129](/var/home/eero/outdoor/Docker/bin/kanban_poll.sh:129),
   [Outdoor launcher:146](/var/home/eero/outdoor/Docker/bin/kanban_poll.sh:146)). Health must be observable without tailing `/tmp` logs.
5. **The Reference Poller's dispatch has the same ambiguity at a simpler level.** Detached delegation
   is marked working before API acceptance is known, and corrupt/missing state loads as empty
   ([AgentDeck poller.py:203](/var/home/eero/agentdeck/.worktrees/research-current-kanban-control-feature-surface/kanban/poller.py:203)). It should consume the shared engine and AgentDeck's stable-delivery worker API rather than grow a second stale-gate protocol.

The current core unit suites passed during this research (`71` Outdoor; `75` Storm, `7` skipped).
Storm adds four config-boundary tests over the copied shared suite
([Storm test_kanban_board.py:22](/var/home/eero/storm/Docker/claude-skills/kanban-poller-storm/test_kanban_board.py:22)). AgentDeck's prototype tests explicitly cover deterministic selection but not side-effecting worktree/dispatch wrappers
([AgentDeck test_kanban_poller.py:1](/var/home/eero/agentdeck/.worktrees/research-current-kanban-control-feature-surface/tests/test_kanban_poller.py:1)). Full-parity extraction therefore needs one shared conformance suite plus integration tests for controller roles, internal label provenance, accepted/uncertain delivery, safe stop, state-loss recovery, worktree registration/cleanup, release/deploy adapters, installed assets, and timer health.

## Reference Poller Project acceptance bar

The AgentDeck Reference Poller Project should be considered complete only when it demonstrates, from
one versioned Poller Manifest plus uncommitted Host Overlay:

1. the shared command/authorization/status/ownership model;
2. FIFO durable controls, generation leases, stable delivery receipts, stop acknowledgement, and
   crash recovery;
3. existing-PR, follow-up, merge-review, merge-fix, fail-closed linked-PR discovery, guarded merge,
   and retryable Done/Blocked transitions;
4. AgentDeck worker backend operation across Web Process deploys, without coupling the Poller
   Instance lifecycle to either the Web Process or Persistent Runtime
   ([CONTEXT.md:246](/var/home/eero/agentdeck/.worktrees/research-current-kanban-control-feature-surface/CONTEXT.md:246));
5. explicit extension examples for AgentDeck tests, screenshots, and safe web/runtime deployment;
6. activation-time validation, install/version verification, structured observability, and a clean
   uninstall/disable path; and
7. conformance fixtures proving the same shared engine can express both current Outdoor and Storm
   policies without source forks.

That makes AgentDeck's repository a real Reference Poller Project—an operational example of manifest,
overlay, extensions, activation, and ongoing operation—not a sample poller or a third engine fork
([CONTEXT.md:251](/var/home/eero/agentdeck/.worktrees/research-current-kanban-control-feature-surface/CONTEXT.md:251)).
