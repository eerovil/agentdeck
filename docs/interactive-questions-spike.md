# Issue #27 — interactive questions & permission prompts (spike findings + plan)

Branch: `feat/claude-interactive-questions`. Goal: surface Claude's multiple-choice
`AskUserQuestion` "special UI" and permission prompts in AgentDeck, and let the user
answer them. Full parity target: multiSelect, freeform "other", permission prompts.

## Spike verdict (all verified live against `claude 2.1.214`, `~/.claude2`)

The **real in-turn control-protocol answer** is feasible for deck-owned `claude -p`
workers. Key facts:

1. **`AskUserQuestion` is NOT in the headless toolset by default.** A plain
   `-p --input-format stream-json` worker cannot call it, and never receives
   permission `control_request`s (Bash just runs under `--permission-mode default`).
2. **`--permission-prompt-tool stdio` is the unlock.** Adding it:
   - puts `AskUserQuestion` into the toolset (verified: absent without the flag,
     present with it), and
   - routes interactive decisions to the client as an inbound
     `control_request` (`subtype: "can_use_tool"`).
3. **Autonomy is preserved.** Under `--permission-mode bypassPermissions` +
   `--permission-prompt-tool stdio`: regular tools (Bash) auto-run with NO
   control_request; only `AskUserQuestion` routes to the client
   (`requires_user_interaction: true`). So autonomous kanban-style workers are
   unaffected; default/interactive accounts additionally get real permission prompts.

### Wire shapes

Inbound (CLI → client), one per interactive decision:
```json
{"type":"control_request","request_id":"<uuid>",
 "request":{"subtype":"can_use_tool","tool_name":"AskUserQuestion",
   "display_name":"AskUserQuestion","tool_use_id":"toolu_...",
   "requires_user_interaction":true,
   "input":{"questions":[{"question":"...","header":"...","multiSelect":false,
     "options":[{"label":"apple","description":"..."}]}]}}}
```
Permission variant: `tool_name` is the real tool (e.g. `Bash`), `input` is the tool
input, plus `description` and `permission_suggestions[]`; no `requires_user_interaction`.

Outbound (client → CLI):
```json
{"type":"control_response","response":{"subtype":"success","request_id":"<uuid>",
  "response":{"behavior":"allow","updatedInput":{...}}}}
// deny: {"behavior":"deny","message":"..."}
```
**AskUserQuestion answer encoding (VERIFIED).** The allow-response schema is fixed
(`{behavior, updatedInput, updatedPermissions, toolUseID?, decisionClassification?}`)
— there is no answer field, and `updatedInput` is re-validated against the original
AskUserQuestion input schema (narrowing `options` below 2 fails). The answer rides
inside `updatedInput` as a top-level `answers` map keyed by **question text**:
```json
{"behavior":"allow",
 "updatedInput":{"questions":[<original, options intact>],
   "answers":{"Pick a fruit":"banana"}}}
```
Verified: tool_result becomes `Your questions have been answered: "Pick a fruit"="banana".`
For multiSelect, `answers` values are strings → join the chosen labels into one string.
Permission (non-AskUserQuestion) prompts use plain `allow`/`deny` (echo `updatedInput`).

## Architecture (4 layers to change)

- `providers/claude_code/worker.py` — the runtime-owned `ClaudeWorkerHost`
  (`claude -p` processes). Add `--permission-prompt-tool stdio` to `_command`; in
  `_read_loop`/`_handle_event` detect inbound `control_request`, hold it pending on
  the `_LiveWorker` (turn is paused, agent blocked on our reply); add an `answer(key,
  interaction_id, answers, decision)` that writes the `control_response`; include the
  pending interaction in `snapshot()`.
- `runtime.py` — HTTP surface over the socket. Add
  `POST /claude/accounts/{label}/answer` (mirror the existing Codex `AnswerRequest`
  but keyed by worker `key`, not `thread_id`); pending interaction already flows via
  the `/workers` snapshot.
- `providers/claude_code/worker_client.py` — web-side facade. Read the pending
  interaction from the snapshot into `_owned`; add `answer(...)`.
- `providers/claude_code/provider.py` — implement `pending_interaction()` (build a
  `PendingInteraction`/`InteractionQuestion` from the stored control_request; kind
  `question` for AskUserQuestion, an approval kind for permissions) and
  `answer_interaction()` (call the client). The existing widget
  (`web/templates/partials/pending_interaction.html`), the
  `POST /sessions/{key}/interaction` endpoint, and the `answer__<id>`/`other__<id>`
  form parsing are all reused unchanged (Codex already drives them).

### Render vs answer
- Render half (parse options → widget) also benefits foreign/observed sessions whose
  transcripts already contain `AskUserQuestion` tool_use blocks (read-only). Ships via
  web-only `agentdeck.service` restart.
- Answer half lives in the persistent Claude worker runtime → needs an
  `agentdeck-codex.service` restart, which interrupts active workers. Sequence the
  deploy carefully (AGENTS.md).
