# Local Ollama Tool-Calling Evaluation

**Date**: 2026-05-06
**Hardware**: Mac M2, 16 GB unified memory (~10.7 GiB available for ollama
inference per `ollama serve` startup log)
**Goal**: Decide which locally-served Ollama model the gateway should
default to for opencode-backed sessions, given that tool-calling
reliability — not raw text quality — is the binding constraint.

This document records the evaluation that produced the current default
recommendation (`mistral-nemo:latest`) and captures the constraints that
shaped the choice. It exists so future maintainers do not repeat the
setup mistakes that consumed several hours during the initial rollout.

## Background: why this evaluation was needed

When the gateway was first wired to opencode, every `/ask` returned
text refusals like *"I don't have a bash function"* even though the
opencode build agent declared 12 tools. Investigation produced several
non-obvious findings:

1. **opencode does not auto-detect a running local Ollama daemon.**
   The models.dev catalog only contains `ollama-cloud` (the hosted
   Turbo service) and `lmstudio`. Local `:11434` must be registered as
   a custom provider (`@ai-sdk/openai-compatible` over
   `http://localhost:11434/v1`).
2. **Minimum provider model metadata is silently insufficient.**
   Specifying only `tool_call: true` on a custom-provider model entry
   leaves opencode's outgoing chat-completion payload without a
   `tools[]` array. The model then hallucinates fake tool names
   (`list`, `run`, `todo`) because it has been told via system prompt
   that tools exist but never receives the schemas. Full
   catalog-style metadata (id, family, attachment, reasoning,
   `tool_call`, temperature, release_date, last_updated, modalities,
   open_weights, cost, limit) is the threshold that flips opencode
   into actually forwarding `tools[]`.
3. **`tools[]` reaching the model is necessary but not sufficient.**
   Even with the schema in hand, sub-14B models routinely refuse to
   emit tool calls or emit them with wrong key names (e.g. `path`
   instead of opencode's required `filePath`).

The evaluation below tested point (3) head-on across every locally
runnable candidate.

## Methodology

### Test bed

A standalone opencode config under `/tmp/oc_tooltest/opencode.json`
declaring all candidate models with full catalog-style metadata. A
forwarding HTTP proxy on `localhost:11500 → :11434` confirmed that
opencode's outgoing `tools[]` payload was correctly populated for these
models (10 tools after build agent filtering: `bash, read, glob, grep,
edit, write, task, webfetch, todowrite, skill`).

The matrix runner (`/tmp/oc_tooltest/run_matrix.sh`) iterates
`MODELS × PROMPTS`, captures stdout per test, and classifies each
result:

- `tool_called` — opencode logged a tool execution marker
  (`✱ Glob`, `$ command`, etc.)
- `schema_fail` — model called the tool but with invalid arguments
- `model_refuse` — model returned text only, never attempted a tool
- `timeout` — opencode killed after `TIMEOUT_SECS` (default 120s,
  raised to 180s for this run)
- `ambiguous` — output did not match heuristics; raw file consulted
  manually for reclassification.

Each invocation was given a 180-second hard timeout via a Python
subprocess wrapper. `OLLAMA_KEEP_ALIVE=10s` was set on the ollama
daemon to evict idle models quickly and prevent RAM thrashing on
M2 16 GB.

### Prompts (escalating strictness)

- **EASY** — single-arg, single-key schema:
  `Use the glob tool with pattern '*.json' to list json files.`
- **MEDIUM** — single-arg but with a non-obvious camelCase key
  (`filePath`, not `path`):
  `Use the read tool to show the file ... The read tool requires a 'filePath' field.`
- **HARD** — two required fields, one of them an audit annotation:
  `Use the bash tool. The bash tool requires TWO JSON fields: command (string) and description (string explaining the command). Run 'ls -1'. JSON arguments MUST include both keys.`

## Results

Reclassified after manual inspection of every `ambiguous` raw file.

| Model | Size | EASY | MEDIUM | HARD | Notes |
|---|---|---|---|---|---|
| `qwen3:14b` | 14 B | timeout (180s) | empty (150s) | timeout (180s) | Stalls under retry loop on schema mismatch |
| `gemma4:latest` | 8 B | timeout (180s) | timeout (180s) | timeout (180s) | Ollama errors out (see Gemma note) |
| `gemma3:4b` | 4 B | unsupported (3s) | unsupported (2s) | unsupported (1s) | `Error: ... does not support tools` |
| `qwen2.5:14b` | 14 B | **tool_called (165s)** | timeout (181s) | timeout (180s) | Single-arg only |
| `llama3.1:8b` | 8 B | refuse (55s) | refuse (82s) | refuse (70s) | Receives `tools[]` but answers in text/code |
| `mistral-nemo:latest` | 12 B | **tool_called (91s)** | empty (100s) | confused (103s) | Single-arg only; HARD veered off-task |

### Observation on Gemma

Ollama itself rejects tool-calling chat completions for Gemma family
models:

```
Error: registry.ollama.ai/library/gemma3:4b does not support tools
```

This is enforced on Ollama's side (model manifest does not advertise
tool support). It is independent of opencode's behavior and applies to
every Gemma variant tested (`gemma3:4b`, `gemma4:latest`). `gemma2:9b`
was not pulled but is expected to behave the same way — Gemma's chat
template upstream does not encode tool calls.

### Observation on llama3.1:8b

`llama3.1:8b` does receive `tools[]` (verified via the proxy log) but
its responses for all three prompts were natural-language descriptions
of *what tool it would call* rather than actual tool calls. Example
EASY response:

```
To answer this question, we can use the following code:
    glob = '/*.json'
    files = glob.glob(pattern)
    print(files)
```

This is a known weakness of 8 B-class instruct models on strict
function-calling protocols.

### Observation on the 14B models

`qwen2.5:14b` and `qwen3:14b` both fit M2 16 GB but are at the upper
edge (~9 GB Q4_K_M plus ~1-2 GB KV cache for the system-prompt-heavy
opencode context). Cold-load times on the first invocation per model
ran 60-90 seconds before any tokens emit; subsequent calls within the
keep-alive window were faster but still tight. `qwen3:14b`
specifically appeared to enter a retry loop when its first tool call
failed schema validation, consuming the entire 180s budget without
ever recovering. `qwen2.5:14b` behaved more deterministically but only
passed the EASY case.

## Decision

**`mistral-nemo:latest` (12 B)** is the recommended default for the
opencode backend on this hardware:

- Only model alongside `qwen2.5:14b` to actually invoke a tool
  (`tool_called` on EASY).
- ~half the wall-clock time of `qwen2.5:14b` (91s vs 165s) due to
  smaller weights.
- Leaves ~3-4 GB of unified memory headroom for KV cache and other
  apps; `qwen2.5:14b` does not.

`qwen2.5:14b` is a viable fallback if a future workload demands
slightly stronger reasoning at the cost of inference speed.

The MEDIUM and HARD prompts were not reliably solved by any tested
model. This is not a hardware ceiling — it is a model-size/training
ceiling for sub-32B Ollama-runnable instruct models. The pragmatic
gateway response is to (a) accept this constraint, (b) shape user
prompts toward single-tool, single-arg formulations, and (c) push
schema hints into the gateway's `PROMPT_PREAMBLE` so every `/ask`
arrives at the model with explicit field-name guidance.

## Hardware ceiling note

Ollama on M2 16 GB exposes ~10.7 GiB of inference memory after the
system reserve. Practical model size implications (Q4_K_M):

| Parameters | VRAM (Q4_K_M) | Status on M2 16 GB |
|---|---|---|
| 7 B | ~4 GB | comfortable |
| 12-14 B | ~7-9 GB | feasible, tight |
| 20 B | ~12 GB | **load fails** (over capacity) |
| 32 B | ~19-20 GB | **cannot run** |

Aggressive quantization (Q3_K_M, Q2_K) can shave 20-30% off these
numbers but degrades quality, and the KV cache for long contexts
narrows the practical headroom further. Bottom line: **14 B is the
realistic local ceiling for this evaluation's hardware**. Stronger
tool-calling capacity per parameter (newer instruct models, dedicated
tool-call fine-tunes) is the most promising avenue, not larger
parameter counts.

## Reproducing the evaluation

```bash
# 1. Test config and runner are at:
#    /tmp/oc_tooltest/opencode.json
#    /tmp/oc_tooltest/run_matrix.sh

# 2. Fast keep-alive so models do not pile up in RAM
osascript -e 'quit app "Ollama"' 2>/dev/null
pkill -f 'Ollama.app|ollama serve' 2>/dev/null
OLLAMA_KEEP_ALIVE=10s /Applications/Ollama.app/Contents/Resources/ollama serve \
  > /tmp/ollama.log 2>&1 &

# 3. Pull candidates (idempotent)
bash codex_gateway/temp_test.sh

# 4. Re-run only the matrix (skip pulls) with a generous timeout
TIMEOUT_SECS=180 bash /tmp/oc_tooltest/run_matrix.sh
```

Outputs land in `/tmp/oc_tooltest/results/<model>__<label>.txt` and
`summary.tsv`. The `temp_test.sh` script in the repo's root is the
one-shot driver and is intended to be deleted once the evaluation is
complete.

## Open questions

- **Will opencode upstream accept tool-call propagation for custom
  openai-compatible providers without requiring full catalog
  metadata?** The current behavior is undocumented and surprising;
  filing an upstream issue is on the to-do list.
- **Discord-MCP for opencode sessions.** The codex backend wires up
  `mcp_servers.discord.env.DISCORD_CHANNEL=...` per session. The
  opencode backend has no equivalent, which is why `discord_notify`
  asks fail silently for opencode-backed sessions. Tracked separately.
- **`/ask` post-completion auto-followup.** Currently the user must
  poll `/last` or enable `/watch on` to retrieve a result; the
  opencode backend now writes `last_response_path` so `/last` works,
  but auto-posting the response into the project channel on
  completion remains unimplemented.

## See also

- [README.md — Local Ollama as an opencode provider](../README.md)
- [scripts/opencode.json.example](../scripts/opencode.json.example) —
  template that becomes `~/.config/opencode/opencode.json`. Uses
  full catalog-style metadata as required.
- [scripts/install.sh](../scripts/install.sh) — checks the user's
  opencode config for a registered `provider.ollama` and prints the
  recommended snippet without overwriting.
