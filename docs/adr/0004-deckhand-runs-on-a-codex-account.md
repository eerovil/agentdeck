# Deckhand runs on a Codex account by construction

Deckhand's attention classifier and Semantic Titles are produced by exec-ing the `codex` CLI
(`deckhand_runner.run_codex_json` runs `codex exec` with `CODEX_HOME` set to the account root), so
`deckhand.deckhand_account` selecting a Codex Provider Account is a real dependency, not a
provider-name leak. There is no Claude-CLI equivalent wired up; a Provider Account that cannot run
`codex exec` cannot host Deckhand. Likewise, account-free machine delegation
(`web.routes_api._delegation_account`) defaults to the sole Codex account to preserve zero-config
`agentdeck delegate` — dropping that default would make a single-account setup ambiguous and break
backward compatibility. Both `provider_id == "codex"` checks are therefore correct domain logic and
do **not** violate ADR-0002's capability-not-provider-name rule, which governs the shared
presentation and control surface — not which account a Codex-specific subsystem must run on. Future
architecture reviews should not re-flag these two sites as leaks.
