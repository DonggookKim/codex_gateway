"""Read-only inspectors for live local state.

- process_inspector: scans running processes for in-flight codex CLIs.
- session_inspector: parses codex rollout files for the latest assistant
  response (used for /status, /last, /current).
"""
