# Development history

This file collects the dev journey, removed features, and rationale that
used to be scattered across README/inline notes. The current spec lives
in [README.md](./README.md). The architecture spec lives in
[DESIGN.md](./DESIGN.md).

## Origins — codex resume --last era

- Single shared codex session driven by `codex resume --last`.
- Discord MCP server lived inside codex itself, configured via
  `[mcp_servers.discord]` in codex config.
- `/ask` would replay the last codex session regardless of which project
  the operator intended.

The MCP-in-codex configuration is documented in
`opencode_discord_remote_dev_reference.md` (the original research
artifact for the eventual opencode migration).

## Multi-project / multi-session refactor

Replaced `resume --last` with explicit project + session selection:

- `ProjectRegistry` holds named projects, each with channel binding,
  default model, and active session.
- `SessionStore` persists per-session metadata (model, codex thread ref,
  blocked reason, etc.) under
  `STATE_ROOT/projects/<project>/sessions/<session>/session.json`.
- Each session owns an isolated `codex-home/.codex`. `auth.json` is
  symlinked from the gateway's shared codex home.
- `/ask` requires both a project and session to be resolvable.

Original spec:
`docs/superpowers/specs/2026-04-22-codex-gateway-multi-session-design.md`.

## Ollama / local-model branch (removed)

There was a path for running local models (e.g. `qwen3-8b`,
`llama3.1:latest`) via codex profiles plus an `execution_env="local_ollama"`
distinction that removed the symlinked `auth.json` so codex would not
treat the local model as a ChatGPT-account request.

This was removed because:

- Codex's sub-agent + local-model story didn't work cleanly.
- Opencode's per-agent model selection covers the same use case better
  through `provider/model` configuration.

Specifically deleted:

- `LOCAL_OLLAMA_ENV`, `LOCAL_ALIAS_PROFILES`
- `discover_ollama_models`, `_parse_ollama_list_output`
- `discovered_ollama_models`, `local_codex_profile`,
  `DEFAULT_LOCAL_CODEX_PROFILE`
- the `qwen3-8b` and `ollama-qwen25-coder` branches in `build_command`
  and `tui_attach.build_resume_argv`
- `_remove_auth_file` (auth is now always linked, never stripped)

The `execution_env` field on `SessionRecord` is retained for backward
JSON-compat with sessions persisted before the deletion; new sessions
get `"openai"` unconditionally.

## OpenCode evaluation

After the codex era, the gateway moved to support opencode as a second
backend. Key findings during the evaluation:

- `opencode run --attach` **auto-rejects every permission ask** in a
  non-interactive context. Driving sessions through `opencode run` as a
  subprocess is therefore not viable for this gateway. The HTTP API on
  `opencode serve` (with SSE for events and POST for permission replies)
  is the right path.
- `opencode session list --format json` works even though `--help` does
  not advertise `--format` on that subcommand.
- Permission asks arrive over SSE as `permission.asked` events with
  `id`, `sessionID`, `permission`, `patterns`, `always`, and the
  triggering `tool.callID` / `messageID`.
- Approval is `POST /session/:id/permissions/:permission_id` with body
  `{"response": "once" | "always" | "reject"}`. Tested via a single
  end-to-end smoke test that started `opencode serve`, sent a
  bash-triggering prompt, intercepted the ask, approved over the HTTP
  API, and verified the bash output appeared in the model response.
- Subagents have independent permission configurations; `task` is
  allowed by default so primary→subagent dispatch does not deadlock
  under unattended operation.

## OpenCode integration

Phase 1 — **Backend abstraction.** Introduced `backend/__init__.py`
with `Backend` ABC, `RunRequest`, and `StopResult`; `backend/codex.py`
became a thin adapter that delegates to the existing `runner.run_codex`
free function so all the codex test patches at module scope kept working.

Phase 2 — **Opencode runtime.** Added:

- `backend/opencode.py` — `OpencodeClient` (REST + SSE), `OpencodeBackend`
  (drives `run()` against an opencode session), `PermissionAsk` event
  parser, `_RunCollector` that aggregates `message.part.delta` /
  `message.part.updated` / `step-finish` / `session.idle`.
- `backend/opencode_server.py` — `OpencodeServer` lifecycle wrapper that
  spawns `opencode serve` (or attaches to a pre-existing instance) and
  parses the `listening on http://...` banner.
- `backend/opencode_runtime.py` — `OpencodeRuntime` holder + an env-driven
  builder so the gateway entrypoint can opt in via env vars only.
- `permission_router.py` — bridges `permission.asked` events to a Discord
  button view (✅ Once / ♾️ Always / ❌ Reject) posted in the project
  channel and an idle-timeout watcher that aborts the opencode session if
  no operator answers within `OPENCODE_IDLE_TIMEOUT_SECONDS`.

Permission UX shape was decided before implementation, naming the four
axes:

- **Input** — buttons + slash fallback (hybrid)
- **Location** — project channel
- **Timeout** — abort the opencode session on operator timeout
- **`always` pattern** — use `permission.asked.always[0]` as-is

Slash fallbacks: `/perms`, `/perm_allow [id?] [scope?]`,
`/perm_reject [id?]`. Without `id` they target the latest pending ask
on the channel-or-globally-active session.

## Channel binding

Originally every command required the control channel. After opencode
shipped, `/ask` and `/session_select` were extended to also work in a
project channel, with the project inferred from the channel binding
(`ProjectDefinition.project_channel_id`). The same change later rolled
out to `/session_list`, `/session_new`, `/model_select`, `/perms`,
`/perm_allow`, `/perm_reject`, `/current`, and `/status`.

In a project channel:

- `/session_select` updates only the project's `active_session_id`,
  leaving the gateway-global selection alone — so two operators in two
  project channels don't trample each other.
- `/session_new` likewise only flips the project's active session.
- `/model_select` writes to a per-project pending map
  (`pending_model_profile_per_project`), persisted in the gateway state
  file.
- `/current` and `/status` render the project's view (its
  `active_runs` entry, `project_last_runs` entry, channel-project's
  active session).

Cross-project commands (`/project_select`, `/project_list`, `/tui`,
`/watch`, `/last`, `/stop`) remain control-channel only.

## Per-project model selection

The global `selected_model_profile_for_new_session` field on
`GatewayState` was replaced by
`pending_model_profile_per_project: dict[str, str]`, which is set by
`/model_select` (per the channel-routed project) and consumed by
`/session_new` only when `backend=opencode`.

Codex sessions intentionally ignore this map and always read
`ProjectDefinition.default_model_profile`, since the codex profile
list is project-specific anyway and `/model_select` is meaningful only
when the gateway has an opencode runtime to pick a `provider/model`
for.

## Earlier validation focus

When the codex multi-session rollout shipped, manual testing focused on:

- `/ask` refuses to run until both project and session are selected.
- Accepted `/ask` messages echo `project/session` and a prompt excerpt.
- `/last` downloads open without garbled text on Android mobile and on
  desktop (utf-8-sig sentinel).
- Blocked / missing-session conditions produce readable operational
  summaries.
- Selection state survives gateway restart through the persisted state
  file.

Later, when opencode shipped, manual testing additionally covered:

- A bash-triggering prompt produces a `permission.asked` event.
- Buttons in the project channel approve/reject via
  `OpencodeClient.reply_permission`.
- Idle timeout aborts the opencode session and updates the message.
- Channel-routed `/session_new`, `/session_select`, `/ask` work in a
  project channel without disturbing other channels' selections.

## References

- `DESIGN.md` — current architecture and persisted state model.
- `opencode_discord_remote_dev_reference.md` — original research artifact
  evaluating opencode for the Discord-driven remote dev use case.
- `tests/` — 147 unit tests covering the codex runner, opencode client /
  backend / server / runtime, permission router, channel routing, and
  per-project model selection.
