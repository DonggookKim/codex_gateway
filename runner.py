from __future__ import annotations

import asyncio
import os
import signal
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import GatewayConfig
from .formatter import excerpt
from .last_response_store import write_last_response_text
from .state import ActiveRun, GatewayState, LastRunSummary, utc_now


class TailBuffer:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.text = ""

    def append(self, chunk: str) -> None:
        self.text = (self.text + chunk)[-self.limit :]


@dataclass
class StopResult:
    attempted: bool
    message: str


def build_wrapped_prompt(config: GatewayConfig, user_prompt: str) -> str:
    prompt = user_prompt.strip()
    if not config.prompt_preamble:
        return prompt
    return f"{config.prompt_preamble}\n\nUser request:\n{prompt}"


def build_command(
    config: GatewayConfig,
    prompt: str,
    last_message_path: Path,
) -> list[str]:
    return [
        config.codex_bin,
        "exec",
        "resume",
        "--last",
        "--skip-git-repo-check",
        "-o",
        str(last_message_path),
        prompt,
    ]


def _signal_name(returncode: int) -> str | None:
    if returncode >= 0:
        return None
    signal_number = -returncode
    try:
        return signal.Signals(signal_number).name
    except ValueError:
        return f"SIG{signal_number}"


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _failed_summary(
    run_id: str,
    requester_user_id: int,
    requester_name: str,
    prompt_excerpt: str,
    reason: str,
) -> LastRunSummary:
    now = utc_now()
    return LastRunSummary(
        run_id=run_id,
        requester_user_id=requester_user_id,
        requester_name=requester_name,
        prompt_excerpt=prompt_excerpt,
        started_at=now,
        finished_at=now,
        exit_code=None,
        exit_signal="SPAWN_FAILED",
        stdout_excerpt="",
        stderr_excerpt=reason,
        assistant_response_excerpt="",
    )


def _prepare_runtime_home(config: GatewayConfig) -> dict[str, str] | None:
    if config.codex_home_parent is None:
        return None

    home_parent = config.codex_home_parent
    codex_dir = home_parent / ".codex"
    if codex_dir.exists():
        shutil.rmtree(codex_dir, ignore_errors=True)
    codex_dir.mkdir(parents=True, exist_ok=True)

    if config.codex_home_seed_from is not None and config.codex_home_seed_from.exists():
        shutil.copytree(
            config.codex_home_seed_from,
            codex_dir,
            dirs_exist_ok=True,
        )

    return {
        "HOME": str(home_parent),
    }


async def _drain_stream(
    stream: asyncio.StreamReader | None,
    buffer: TailBuffer,
    on_update,
) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(1024)
        if not chunk:
            return
        buffer.append(chunk.decode("utf-8", errors="replace"))
        on_update(buffer.text)


async def run_codex(
    state: GatewayState,
    config: GatewayConfig,
    requester_user_id: int,
    requester_name: str,
    prompt: str,
) -> LastRunSummary:
    run_id = uuid.uuid4().hex[:8]
    prompt_excerpt = excerpt(prompt, config.status_text_max_chars)
    started_at = utc_now()
    last_message_path = config.tmp_dir / f"{run_id}-last-message.txt"
    wrapped_prompt = build_wrapped_prompt(config, prompt)
    argv = build_command(config, wrapped_prompt, last_message_path)

    try:
        child_env = None
        runtime_home_env = _prepare_runtime_home(config)
        if runtime_home_env is not None:
            child_env = dict(os.environ)
            child_env.update(runtime_home_env)

        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(config.codex_cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
        )
    except OSError as exc:
        summary = _failed_summary(
            run_id=run_id,
            requester_user_id=requester_user_id,
            requester_name=requester_name,
            prompt_excerpt=prompt_excerpt,
            reason=str(exc),
        )
        state.finish_run(summary)
        return summary

    active_run = ActiveRun(
        run_id=run_id,
        requester_user_id=requester_user_id,
        requester_name=requester_name,
        prompt_excerpt=prompt_excerpt,
        started_at=started_at,
        pid=process.pid,
        last_message_path=last_message_path,
        process=process,
    )
    state.set_active_run(active_run)

    stdout_buffer = TailBuffer(config.stream_tail_chars)
    stderr_buffer = TailBuffer(config.stream_tail_chars)

    stdout_task = asyncio.create_task(
        _drain_stream(
            process.stdout,
            stdout_buffer,
            lambda value: setattr(active_run, "stdout_tail", value),
        )
    )
    stderr_task = asyncio.create_task(
        _drain_stream(
            process.stderr,
            stderr_buffer,
            lambda value: setattr(active_run, "stderr_tail", value),
        )
    )

    active_run.task = asyncio.current_task()

    returncode = await process.wait()
    await asyncio.gather(stdout_task, stderr_task)
    assistant_response_text = _read_text_file(last_message_path)
    write_last_response_text(config.last_response_file, assistant_response_text)

    summary = LastRunSummary(
        run_id=run_id,
        requester_user_id=requester_user_id,
        requester_name=requester_name,
        prompt_excerpt=prompt_excerpt,
        started_at=started_at,
        finished_at=utc_now(),
        exit_code=returncode if returncode >= 0 else None,
        exit_signal=_signal_name(returncode),
        stdout_excerpt=excerpt(stdout_buffer.text, config.status_text_max_chars),
        stderr_excerpt=excerpt(stderr_buffer.text, config.status_text_max_chars),
        assistant_response_excerpt=excerpt(
            assistant_response_text,
            config.status_text_max_chars,
        ),
    )
    state.finish_run(summary)
    return summary


async def stop_active_run(
    state: GatewayState,
    config: GatewayConfig,
) -> StopResult:
    active_run = state.active_run
    if active_run is None or active_run.process is None:
        return StopResult(
            attempted=False,
            message="No active gateway-managed Codex run to stop.",
        )

    process = active_run.process
    if process.returncode is not None:
        return StopResult(
            attempted=False,
            message="The active gateway-managed Codex run already finished.",
        )

    state.mark_stopping()
    process.send_signal(signal.SIGINT)

    try:
        await asyncio.wait_for(
            process.wait(),
            timeout=config.stop_sigint_grace_seconds,
        )
        return StopResult(
            attempted=True,
            message=(
                f"Stop requested for run `{active_run.run_id}` with `SIGINT`. "
                "The subprocess exited during the grace period."
            ),
        )
    except asyncio.TimeoutError:
        pass

    if process.returncode is None:
        process.terminate()

    try:
        await asyncio.wait_for(
            process.wait(),
            timeout=config.stop_sigterm_grace_seconds,
        )
        return StopResult(
            attempted=True,
            message=(
                f"Stop escalated for run `{active_run.run_id}` to `SIGTERM`. "
                "The subprocess then exited."
            ),
        )
    except asyncio.TimeoutError:
        return StopResult(
            attempted=True,
            message=(
                f"Stop was requested for run `{active_run.run_id}`, but the "
                "subprocess did not exit after SIGINT and SIGTERM."
            ),
        )
