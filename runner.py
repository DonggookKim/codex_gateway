from __future__ import annotations

import asyncio
import json
import os
import signal
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import GatewayConfig
from .execution_env import LOCAL_OLLAMA_ENV, OPENAI_ENV
from .formatter import excerpt
from .last_response_store import write_last_response_text
from .notification_router import build_project_notification
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


@dataclass(frozen=True)
class CodexExecutionSettings:
    home_parent: Path | None
    seed_from: Path | None
    tmp_dir: Path
    last_response_file: Path
    use_shared_auth: bool
    shared_auth_source: Path | None = None


class SessionMetaTracker:
    def __init__(self) -> None:
        self.session_id: str | None = None
        self._carry = ""

    def feed(self, chunk: str) -> None:
        self._carry += chunk
        while "\n" in self._carry:
            line, self._carry = self._carry.split("\n", 1)
            if self.session_id is not None:
                continue
            self._consume_line(line)

    def _consume_line(self, line: str) -> None:
        cleaned = line.strip()
        if not cleaned:
            return
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError:
            return
        payload_type = payload.get("type")
        if payload_type == "session_meta":
            meta = payload.get("payload", {})
            if not isinstance(meta, dict):
                return
            session_id = meta.get("id")
            if session_id is not None:
                self.session_id = str(session_id)
            return
        if payload_type == "thread.started":
            thread_id = payload.get("thread_id")
            if thread_id is not None:
                self.session_id = str(thread_id)


def build_wrapped_prompt(config: GatewayConfig, user_prompt: str) -> str:
    prompt = user_prompt.strip()
    if not config.prompt_preamble:
        return prompt
    return f"{config.prompt_preamble}\n\nUser request:\n{prompt}"


def build_command(
    codex_bin: str,
    session_ref: str | None,
    prompt: str,
    last_message_path: Path,
    discord_channel_name: str | None = None,
    model_profile: str | None = None,
    local_model_profiles: set[str] | tuple[str, ...] | None = None,
    local_codex_profile: str = "ollama-qwen25-coder",
) -> list[str]:
    command = [
        codex_bin,
        "exec",
    ]
    exec_level_options: list[str] = []
    resume_level_options: list[str] = []
    normalized_local_models = set(local_model_profiles or ())
    if model_profile == "qwen3-8b":
        exec_level_options.extend(["-p", local_codex_profile])
        resume_level_options.extend(["-m", "qwen3:8b"])
    elif model_profile in normalized_local_models:
        exec_level_options.extend(["-p", local_codex_profile])
        resume_level_options.extend(["-m", model_profile])
    elif model_profile:
        resume_level_options.extend(["-m", model_profile])
    command.extend(exec_level_options)
    if not session_ref:
        command.extend(["--json", "--skip-git-repo-check"])
    else:
        command.extend(["resume", session_ref, "--skip-git-repo-check"])
    if discord_channel_name:
        command.extend(
            [
                "-c",
                "mcp_servers.discord.env.DISCORD_CHANNEL="
                + json.dumps(discord_channel_name, ensure_ascii=False),
            ]
        )
    command.extend(resume_level_options)
    command.extend(["-o", str(last_message_path), prompt])
    return command


def _resolve_session_ref(state: GatewayState, session_ref: str | None = None) -> str:
    resolved = (
        session_ref
        or state.selection_state.get("selected_session_id")
        or ""
    ).strip()
    if not resolved:
        raise ValueError("No selected session available for Codex resume")
    return resolved


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


def _append_text_file(path: Path, chunk: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8-sig") as handle:
        handle.write(chunk)


def _write_debug_artifact(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )


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


def _blocked_summary(
    run_id: str,
    requester_user_id: int,
    requester_name: str,
    prompt_excerpt: str,
    reason: str,
    *,
    exit_signal: str = "BLOCKED",
) -> LastRunSummary:
    summary = _failed_summary(
        run_id=run_id,
        requester_user_id=requester_user_id,
        requester_name=requester_name,
        prompt_excerpt=prompt_excerpt,
        reason=reason,
    )
    summary.exit_signal = exit_signal
    return summary


def _emit_project_notification(
    notification_sink: Callable[[str], None] | None,
    project_label: str | None,
    session_label: str | None,
    reason_code: str,
    explanation: str,
    recovery_hint: str,
) -> None:
    if notification_sink is None:
        return
    if not project_label or not session_label:
        return
    notification_sink(
        build_project_notification(
            project_label=project_label,
            session_label=session_label,
            reason_code=reason_code,
            explanation=explanation,
            recovery_hint=recovery_hint,
        )
    )


def _resolve_shared_auth_source(
    explicit_source: Path | None = None,
) -> Path | None:
    if explicit_source is not None:
        return explicit_source
    raw = os.environ.get("OPENAI_SHARED_AUTH_SOURCE", "").strip()
    if raw:
        return Path(raw).expanduser()
    raw = os.environ.get("CODEX_SHARED_AUTH_SOURCE", "").strip()
    if raw:
        return Path(raw).expanduser()
    default = Path.home() / ".codex" / "auth.json"
    return default if default.exists() else None


def _ensure_shared_auth_link(
    codex_dir: Path,
    *,
    shared_auth_source: Path | None = None,
) -> None:
    shared_auth_source = _resolve_shared_auth_source(shared_auth_source)
    if shared_auth_source is None or not shared_auth_source.exists():
        return

    target = codex_dir / "auth.json"
    try:
        if target.exists() or target.is_symlink():
            if target.resolve() == shared_auth_source.resolve():
                return
            target.unlink()
    except OSError:
        if target.exists() or target.is_symlink():
            target.unlink()

    target.symlink_to(shared_auth_source)


def _remove_auth_file(codex_dir: Path) -> None:
    target = codex_dir / "auth.json"
    if not target.exists() and not target.is_symlink():
        return
    target.unlink()


def prepare_runtime_home_dir(
    *,
    home_parent: Path,
    seed_from: Path | None,
    shared_auth_source: Path | None = None,
    use_shared_auth: bool = True,
) -> None:
    codex_dir = home_parent / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)

    if seed_from is not None and seed_from.exists():
        shutil.copytree(
            seed_from,
            codex_dir,
            dirs_exist_ok=True,
        )

    if use_shared_auth:
        _ensure_shared_auth_link(
            codex_dir,
            shared_auth_source=shared_auth_source,
        )
    else:
        _remove_auth_file(codex_dir)


def _prepare_runtime_home(config: GatewayConfig) -> dict[str, str] | None:
    if config.codex_home_parent is None:
        return None

    home_parent = config.codex_home_parent
    prepare_runtime_home_dir(
        home_parent=home_parent,
        seed_from=config.codex_home_seed_from,
        shared_auth_source=_resolve_shared_auth_source(),
    )

    return {
        "HOME": str(home_parent),
    }


def _resolve_execution_settings(
    config: GatewayConfig,
    *,
    execution_env: str,
    codex_home_path: Path | None,
    runtime_root: Path | None,
    last_response_file: Path | None,
) -> CodexExecutionSettings:
    tmp_dir = (
        runtime_root / "tmp"
        if runtime_root is not None
        else config.tmp_dir
    )
    tmp_dir.mkdir(parents=True, exist_ok=True)
    seed_from = config.codex_home_seed_from
    use_shared_auth = execution_env != LOCAL_OLLAMA_ENV
    shared_auth_source = (
        _resolve_shared_auth_source() if use_shared_auth else None
    )
    return CodexExecutionSettings(
        home_parent=codex_home_path.parent if codex_home_path is not None else None,
        seed_from=seed_from,
        tmp_dir=tmp_dir,
        last_response_file=last_response_file or config.last_response_file,
        use_shared_auth=use_shared_auth,
        shared_auth_source=shared_auth_source,
    )


def _prepare_execution_home(
    settings: CodexExecutionSettings,
) -> dict[str, str] | None:
    if settings.home_parent is None:
        return None

    prepare_runtime_home_dir(
        home_parent=settings.home_parent,
        seed_from=settings.seed_from,
        shared_auth_source=settings.shared_auth_source,
        use_shared_auth=settings.use_shared_auth,
    )
    return {"HOME": str(settings.home_parent)}


async def _drain_stream(
    stream: asyncio.StreamReader | None,
    buffer: TailBuffer,
    on_update,
    chunk_sink: Callable[[str], None] | None = None,
) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(1024)
        if not chunk:
            return
        decoded = chunk.decode("utf-8", errors="replace")
        buffer.append(decoded)
        on_update(buffer.text)
        if chunk_sink is not None:
            chunk_sink(decoded)


async def run_codex(
    state: GatewayState,
    config: GatewayConfig,
    requester_user_id: int,
    requester_name: str,
    prompt: str,
    project_id: str | None = None,
    session_id: str | None = None,
    session_ref: str | None = None,
    start_new_session: bool = False,
    discord_channel_name: str | None = None,
    model_profile: str | None = None,
    execution_env: str = OPENAI_ENV,
    codex_home_path: Path | None = None,
    runtime_root: Path | None = None,
    last_response_file: Path | None = None,
    project_label: str | None = None,
    session_label: str | None = None,
    project_notification_sink: Callable[[str], None] | None = None,
) -> LastRunSummary:
    run_id = uuid.uuid4().hex[:8]
    prompt_excerpt = excerpt(prompt, config.status_text_max_chars)
    started_at = utc_now()
    execution_settings = _resolve_execution_settings(
        config,
        execution_env=execution_env,
        codex_home_path=codex_home_path,
        runtime_root=runtime_root,
        last_response_file=last_response_file,
    )
    last_message_path = execution_settings.last_response_file
    stderr_artifact_path = execution_settings.tmp_dir / f"{run_id}-stderr.txt"
    debug_artifact_path = execution_settings.tmp_dir / f"{run_id}-debug.json"
    wrapped_prompt = build_wrapped_prompt(config, prompt)
    resolved_session_ref: str | None = None
    if not start_new_session:
        try:
            resolved_session_ref = _resolve_session_ref(state, session_ref)
        except ValueError as exc:
            summary = _blocked_summary(
                run_id=run_id,
                requester_user_id=requester_user_id,
                requester_name=requester_name,
                prompt_excerpt=prompt_excerpt,
                reason=str(exc),
            )
            state.finish_run(summary)
            _emit_project_notification(
                project_notification_sink,
                project_label,
                session_label,
                reason_code="missing_session",
                explanation=str(exc),
                recovery_hint="Select a session before retrying the prompt.",
            )
            return summary
    argv = build_command(
        config.codex_bin,
        resolved_session_ref,
        wrapped_prompt,
        last_message_path,
        discord_channel_name,
        model_profile,
        set(config.discovered_ollama_models),
        config.local_codex_profile,
    )
    child_env = None
    runtime_home_env = _prepare_execution_home(execution_settings)
    if runtime_home_env is not None:
        child_env = dict(os.environ)
        child_env.update(runtime_home_env)
    _write_debug_artifact(
        debug_artifact_path,
        {
            "run_id": run_id,
            "project_id": project_id,
            "session_id": session_id,
            "session_ref": resolved_session_ref,
            "model_profile": model_profile,
            "execution_env": execution_env,
            "argv": argv,
            "cwd": str(config.codex_cwd),
            "home": (
                runtime_home_env.get("HOME")
                if runtime_home_env is not None
                else os.environ.get("HOME")
            ),
            "started_at": started_at.isoformat(),
        },
    )

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(config.codex_cwd),
            stdin=asyncio.subprocess.DEVNULL,
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
        _emit_project_notification(
            project_notification_sink,
            project_label,
            session_label,
            reason_code="spawn_failed",
            explanation=str(exc),
            recovery_hint="Check the gateway logs and retry the prompt.",
        )
        return summary

    active_run = ActiveRun(
        run_id=run_id,
        requester_user_id=requester_user_id,
        requester_name=requester_name,
        prompt_excerpt=prompt_excerpt,
        started_at=started_at,
        pid=process.pid,
        last_message_path=last_message_path,
        project_id=project_id,
        session_id=session_id,
        model_profile=model_profile,
        process=process,
    )
    state.set_active_run(active_run)

    stdout_buffer = TailBuffer(config.stream_tail_chars)
    stderr_buffer = TailBuffer(config.stream_tail_chars)
    session_meta_tracker = SessionMetaTracker() if start_new_session else None

    stdout_task = asyncio.create_task(
        _drain_stream(
            process.stdout,
            stdout_buffer,
            lambda value: setattr(active_run, "stdout_tail", value),
            session_meta_tracker.feed if session_meta_tracker is not None else None,
        )
    )
    stderr_task = asyncio.create_task(
        _drain_stream(
            process.stderr,
            stderr_buffer,
            lambda value: setattr(active_run, "stderr_tail", value),
            lambda chunk: _append_text_file(stderr_artifact_path, chunk),
        )
    )

    active_run.task = asyncio.current_task()

    returncode = await process.wait()
    await asyncio.gather(stdout_task, stderr_task)
    assistant_response_text = _read_text_file(last_message_path)
    write_last_response_text(
        execution_settings.last_response_file,
        assistant_response_text,
    )

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
        codex_thread_ref=(
            session_meta_tracker.session_id
            if session_meta_tracker is not None
            else resolved_session_ref
        ),
    )
    _write_debug_artifact(
        debug_artifact_path,
        {
            "run_id": run_id,
            "project_id": project_id,
            "session_id": session_id,
            "session_ref": resolved_session_ref,
            "model_profile": model_profile,
            "execution_env": execution_env,
            "argv": argv,
            "cwd": str(config.codex_cwd),
            "home": (
                runtime_home_env.get("HOME")
                if runtime_home_env is not None
                else os.environ.get("HOME")
            ),
            "started_at": started_at.isoformat(),
            "finished_at": summary.finished_at.isoformat()
            if summary.finished_at is not None
            else None,
            "exit_code": summary.exit_code,
            "exit_signal": summary.exit_signal,
        },
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
