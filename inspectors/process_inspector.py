from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CodexProcessInfo:
    pid: int
    cwd: str
    argv0: str
    command: str


def _safe_realpath(path: str) -> str:
    try:
        return str(Path(path).resolve())
    except OSError:
        return path


def find_codex_processes(
    target_cwd: Path,
    exclude_pids: set[int] | None = None,
) -> list[CodexProcessInfo]:
    excluded = set(exclude_pids or set())
    target = _safe_realpath(str(target_cwd))
    results: list[CodexProcessInfo] = []

    for pid_text in os.listdir("/proc"):
        if not pid_text.isdigit():
            continue
        pid = int(pid_text)
        if pid in excluded:
            continue

        proc_dir = Path("/proc") / pid_text
        try:
            cwd = os.readlink(proc_dir / "cwd")
            cwd_real = _safe_realpath(cwd)
            if cwd_real != target:
                continue

            raw = (proc_dir / "cmdline").read_bytes()
        except (OSError, PermissionError):
            continue

        argv = [
            part.decode("utf-8", errors="replace")
            for part in raw.split(b"\x00")
            if part
        ]
        if not argv:
            continue

        argv0 = Path(argv[0]).name
        if "codex" not in argv0:
            continue
        if "codex_gateway" in " ".join(argv):
            continue

        results.append(
            CodexProcessInfo(
                pid=pid,
                cwd=cwd_real,
                argv0=argv0,
                command=" ".join(argv),
            )
        )

    results.sort(key=lambda item: item.pid)
    return results
