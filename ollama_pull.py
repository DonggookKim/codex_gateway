"""Ollama model availability + on-demand pull helper.

Used by `/model_select` so the user can name a tag that has not yet been
pulled — the gateway transparently runs `ollama pull <tag>` and reports
progress instead of failing later when opencode tries to talk to a
missing model.

The helper is deliberately small and side-effect-confined:
- shells out to the `ollama` CLI (asyncio subprocess, never shell=True)
- does not modify any persisted state itself
- yields plain status strings that callers route to Discord followups

Validation: a regex guards the tag input so a user-supplied string can
never be reinterpreted as additional CLI args. (asyncio.create_subprocess_exec
already passes argv as a list, but the regex makes the contract explicit
and lets tests stay strict.)
"""
from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass
from typing import AsyncIterator


# Allow the characters Ollama tag names can legitimately use:
# letters, digits, ':', '-', '_', '.', '/'. Anything else is rejected.
_TAG_PATTERN = re.compile(r"^[A-Za-z0-9._:/-]+$")


class OllamaPullError(RuntimeError):
    """Raised when an ollama pull cannot proceed (binary missing, bad tag,
    or the CLI exited non-zero)."""


@dataclass(frozen=True)
class _PullStatus:
    """One status snapshot emitted while a pull is in progress.

    `done` is True for the final event (success or failure)."""

    message: str
    done: bool
    success: bool


def _resolve_ollama_bin(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    found = shutil.which("ollama")
    if not found:
        raise OllamaPullError(
            "ollama CLI not found on PATH; install Ollama or set OLLAMA_BIN"
        )
    return found


def _validate_tag(tag: str) -> str:
    cleaned = tag.strip()
    if not cleaned:
        raise OllamaPullError("ollama tag must not be empty")
    if not _TAG_PATTERN.match(cleaned):
        raise OllamaPullError(
            f"ollama tag {cleaned!r} contains characters outside [A-Za-z0-9._:/-]"
        )
    return cleaned


async def list_local_models(*, ollama_bin: str | None = None) -> list[str]:
    """Return the set of locally-pulled model tags via `ollama list`."""
    binary = _resolve_ollama_bin(ollama_bin)
    proc = await asyncio.create_subprocess_exec(
        binary,
        "list",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _stderr = await proc.communicate()
    if proc.returncode != 0:
        return []
    out: list[str] = []
    for raw in stdout.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("name "):
            continue
        first = line.split()[0]
        if first and first not in out:
            out.append(first)
    return out


async def is_pulled(tag: str, *, ollama_bin: str | None = None) -> bool:
    """True if `tag` already appears in `ollama list`."""
    cleaned = _validate_tag(tag)
    return cleaned in await list_local_models(ollama_bin=ollama_bin)


async def pull(
    tag: str,
    *,
    ollama_bin: str | None = None,
    progress_interval_seconds: float = 5.0,
) -> AsyncIterator[_PullStatus]:
    """Run `ollama pull <tag>` and yield status snapshots.

    Yields exactly one snapshot every `progress_interval_seconds` while
    the pull is in flight, plus one final terminal snapshot. Callers
    consume it with `async for` and post each `.message` to Discord.

    The helper does not retry; transient ollama failures bubble up as
    a final snapshot with `success=False`.
    """
    cleaned = _validate_tag(tag)
    binary = _resolve_ollama_bin(ollama_bin)

    proc = await asyncio.create_subprocess_exec(
        binary,
        "pull",
        cleaned,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    last_line_holder: dict[str, str] = {"value": ""}

    async def _drain() -> None:
        if proc.stdout is None:
            return
        try:
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    last_line_holder["value"] = line
        except asyncio.CancelledError:
            raise
        except Exception:
            # Best-effort drain; failures are surfaced via the wait below.
            pass

    drain_task = asyncio.create_task(_drain())

    async def _emit() -> AsyncIterator[_PullStatus]:
        try:
            while True:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(proc.wait()),
                        timeout=progress_interval_seconds,
                    )
                    break
                except asyncio.TimeoutError:
                    snapshot = last_line_holder["value"] or "pull in progress"
                    yield _PullStatus(
                        message=f"`ollama pull {cleaned}` — {snapshot}",
                        done=False,
                        success=False,
                    )
        finally:
            drain_task.cancel()
            try:
                await drain_task
            except (asyncio.CancelledError, Exception):
                pass

        rc = proc.returncode
        if rc == 0:
            yield _PullStatus(
                message=f"✅ `ollama pull {cleaned}` complete",
                done=True,
                success=True,
            )
        else:
            tail = last_line_holder["value"] or f"exit code {rc}"
            yield _PullStatus(
                message=f"❌ `ollama pull {cleaned}` failed: {tail}",
                done=True,
                success=False,
            )

    async for status in _emit():
        yield status


async def ensure_pulled(
    tag: str,
    *,
    ollama_bin: str | None = None,
    progress_sink=None,
    progress_interval_seconds: float = 5.0,
) -> bool:
    """High-level: pull `tag` if missing, route progress to `progress_sink`.

    `progress_sink` is an optional async callable accepting one string per
    status snapshot (for piping into Discord followups). Returns True on
    success, False on failure. Raises `OllamaPullError` only for setup
    problems (bad tag, missing CLI).
    """
    cleaned = _validate_tag(tag)
    if await is_pulled(cleaned, ollama_bin=ollama_bin):
        if progress_sink is not None:
            await progress_sink(f"ℹ️ `{cleaned}` already pulled; skipping download")
        return True

    if progress_sink is not None:
        await progress_sink(
            f"⬇️ `{cleaned}` not found locally — running `ollama pull`. "
            "This may take a few minutes."
        )

    # Only the start (above) and the terminal status (below) are surfaced.
    # The interval snapshots from `pull()` are still consumed so the
    # subprocess gets drained, but we do not flood Discord with each
    # download-percentage tick.
    success = False
    async for status in pull(
        cleaned,
        ollama_bin=ollama_bin,
        progress_interval_seconds=progress_interval_seconds,
    ):
        if status.done:
            if progress_sink is not None:
                await progress_sink(status.message)
            success = status.success
    return success
