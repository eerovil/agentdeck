# AgentDeck kanban poller

An autonomous issue → PR pipeline for this repo. A `--user` systemd timer runs a
**token-free** poll every ~30s; whenever an open issue is labelled `agent` and
isn't already being worked, the poll spins up an **isolated git worktree** and
dispatches a **local AgentDeck worker** (an `agentdeck delegate` Codex chat) that
implements the change, validates it, and opens a PR against `master` — on its own.

Merging a PR to GitHub `master` deploys automatically on the next poll. The
poller fast-forwards only a clean local `master`, syncs dependencies, restarts
and health-checks the web process, and records that phase separately. When the
change touches persistent-runtime-owned code, its restart is deferred until the
runtime reports that every owned Codex and Claude turn is idle.

## How it works

```
systemd timer ─▶ kanban_poll.sh ─(flock)─▶ poller.py
                                              │  gh issue list --label agent   (no LLM)
                                              │  gh pr list                    (skip PR'd)
                                              │  state file + stale gate       (skip in-flight)
                                              ▼
                        for each new issue (up to `concurrency`):
                          git worktree add .worktrees/issue-<n>  +  uv sync
                          agentdeck delegate --cwd <worktree>  <  worker_prompt.md   (detached)
                                              │
                          local AgentDeck Codex worker:
                            implement ▶ .venv/bin/pytest ▶ ruff ▶ shot.py ▶ gh pr create (Closes #n)
```

- **Token-free at rest:** `poller.py` is plain `gh`/`git`/`uv`; the only token spend
  is a dispatched worker, and only when there's real work.
- **Isolation:** one worktree per issue under `.worktrees/issue-<n>`, each with its
  own `.venv`. The Playwright browser tests run in-process (ASGI, no bound port or
  DB), so parallel workers never collide.
- **Author guard (public repo):** an issue's body becomes the worker's prompt, so the
  poll only acts on issues opened by an allow-listed author (`allowed_authors` in
  `config.json`, default `["eerovil"]`). Issues from anyone else are ignored even if
  they carry the `agent` label.
- **Idempotency:** `.kanban/state.json` (gitignored) records each dispatch. An issue
  is skipped while a worker is in flight (`< stale_hours`), permanently once it has
  an open PR, and becomes eligible again if a worker dies and goes stale.
- **Retryable deploys:** `.kanban/deploy.json` records the web and runtime revisions
  independently. Failed health checks retry; runtime changes wait across polls
  without delaying the safe web deployment.
- **Concurrency:** up to `concurrency` (default 2) workers at once.
- **Screenshots:** for UI-touching diffs the worker runs `shot.py`, which renders the
  affected pages in-process and uploads PNGs to the `kanban-shots` release, embedding
  them in the PR. Screenshots are evidence: workers exercise the feature and capture
  representative used states and meaningful transitions, rather than submitting a
  generic page that only happens to contain the changed UI.

## Files

| File | Role |
| --- | --- |
| `config.json` | repo, label, paths, base branch, concurrency, deploy settings |
| `poller.py` | the engine — select issues, make worktrees, dispatch workers |
| `kanban_poll.sh` | flocked launcher run by the timer |
| `worker_prompt.md` | the instructions handed to each worker delegation |
| `shot.py` | in-process page screenshotter → `kanban-shots` release |
| `systemd/*.{service,timer}` | the user timer that drives the poll |

## Install / enable

```sh
# from the live checkout
cd /home/eero/agentdeck
ln -sf "$PWD/kanban/systemd/agentdeck-kanban-poll.service" ~/.config/systemd/user/
ln -sf "$PWD/kanban/systemd/agentdeck-kanban-poll.timer"   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agentdeck-kanban-poll.timer
```

## Operate

```sh
# See what it WOULD dispatch, without doing anything:
kanban/kanban_poll.sh --dry-run              # or: python kanban/poller.py --dry-run --verbose

systemctl --user list-timers | grep kanban   # next fire
tail -f /tmp/agentdeck-kanban.log            # poll + worker delegate logs
cat /home/eero/agentdeck/.kanban/state.json  # dispatch ledger
git worktree list                            # active issue worktrees

systemctl --user stop agentdeck-kanban-poll.timer     # pause the pipeline
```

To hand an issue to the poller, just add the **`agent`** label. Clean up a finished
worktree with `git worktree remove .worktrees/issue-<n>` after its PR is merged.
