# codex_gateway

Discord control-plane gateway for a local Codex CLI session.

This directory is for the lightweight remote-control bridge that listens in a
dedicated Discord control channel and runs:

```bash
codex exec resume --last --skip-git-repo-check "<prompt>"
```

The existing Discord MCP server is intentionally kept separate. In this design:

- The Codex Discord MCP remains the egress path for Codex-originated questions
  and reports in the existing `일반` channel.
- The new gateway bot is the ingress path for user-issued control commands in a
  separate `control` channel.

## v1 scope

- `/ask <prompt>`
- `/status`
- `/last`
- `/stop`

## Documents

- [DESIGN.md](./DESIGN.md): v1 architecture, command contracts, state model, and
  failure handling.

## Quick start

1. Keep the existing root [init_mcp_server.sh](../init_mcp_server.sh) for the
   Codex Discord MCP server in `일반`.
2. Copy `.env.example` to `.env` and fill in the control channel IDs.
3. Install Python dependencies:

   ```bash
   pip install -r codex_gateway/requirements.txt
   ```

4. Run the gateway:

   ```bash
   bash codex_gateway/run_gateway.sh
   ```

The gateway bot is intended to run alongside the existing Discord MCP server,
not replace it.

## Session visibility

Discord `/ask` uses `codex exec resume --last`, so it appends to the most recent
Codex session on disk.

The important limitation is that this is still a separate non-interactive Codex
process. It updates the same session history on disk, but it does not live-drive
an already-open TUI window.

If you later reopen Codex with plain `codex`, you may start a fresh interactive
session and not see the remote follow-up in that new TUI. To inspect the same
ongoing session after remote `/ask` usage, reopen with:

```bash
codex resume --last
```

## Current design decisions

- Single project only for v1.
- Single active `/ask` execution at a time.
- `/stop` only targets the active gateway-managed subprocess.
- `/status` prefers the latest assistant response found in Codex session logs.
- `/last` attaches the latest full assistant response as `last_response.txt`.
- `/ask` posts a short completion message with only a brief last-response preview.
- `/status` also shows only a brief last-response preview; full text is reserved for `/last`.
