from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


DEFAULT_PROMPT_PREAMBLE = """You are being resumed via a Discord control gateway.
Prefer returning your final answer in the CLI final response.
Avoid Discord MCP messages unless the task genuinely requires them.
"""

DEFAULT_STATE_ROOT = Path("/tmp") / "codex_gateway_state"
DEFAULT_RUNTIME_ROOT = Path.home() / "codex_gateway_runtime"
DEFAULT_PROJECTS_FILE = DEFAULT_STATE_ROOT / "projects.json"
DEFAULT_STATE_FILE = DEFAULT_STATE_ROOT / "gateway_state.json"
DEFAULT_TMP_DIR = DEFAULT_RUNTIME_ROOT / "tmp"
DEFAULT_LAST_RESPONSE_FILE = DEFAULT_TMP_DIR / "last_response.txt"
DEFAULT_LOCAL_CODEX_PROFILE = "ollama-qwen25-coder"


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _first_present_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    joined = ", ".join(names)
    raise ValueError(f"Missing required environment variable. Tried: {joined}")


def _int_env(name: str, default: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None:
        if default is None:
            raise ValueError(f"Missing required environment variable: {name}")
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer") from exc


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be a float") from exc


def _path_env(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return Path(raw).expanduser()


def _csv_ints(raw: str) -> set[int]:
    values: set[int] = set()
    for token in raw.split(","):
        item = token.strip()
        if not item:
            continue
        try:
            values.add(int(item))
        except ValueError as exc:
            raise ValueError(
                "ALLOWED_USER_IDS must be a comma-separated list of integers"
            ) from exc
    if not values:
        raise ValueError("ALLOWED_USER_IDS must contain at least one user ID")
    return values


def _parse_ollama_list_output(raw: str) -> tuple[str, ...]:
    models: list[str] = []
    for line in raw.splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.lower().startswith("name "):
            continue
        model = cleaned.split()[0].strip()
        if model and model not in models:
            models.append(model)
    return tuple(models)


def discover_ollama_models() -> tuple[str, ...]:
    commands: list[list[str]] = []
    configured_ollama_bin = os.environ.get("OLLAMA_BIN", "").strip()
    if configured_ollama_bin:
        commands.append([configured_ollama_bin, "list"])
    else:
        discovered_bin = shutil.which("ollama")
        if discovered_bin:
            commands.append([discovered_bin, "list"])
        commands.append(["cmd.exe", "/c", "ollama", "list"])

    for command in commands:
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            continue
        if result.returncode != 0:
            continue
        models = _parse_ollama_list_output(result.stdout)
        if models:
            return models
    return ()


@dataclass(frozen=True)
class GatewayConfig:
    discord_gateway_token: str
    control_guild_id: int
    control_channel_id: int
    allowed_user_ids: set[int]
    codex_bin: str
    codex_cwd: Path
    codex_home_parent: Path | None
    codex_home_seed_from: Path | None
    codex_status_home: Path
    prompt_max_chars: int
    status_text_max_chars: int
    stream_tail_chars: int
    stop_sigint_grace_seconds: float
    stop_sigterm_grace_seconds: float
    response_preview_chars: int
    state_root: Path = DEFAULT_STATE_ROOT
    runtime_root: Path = DEFAULT_RUNTIME_ROOT
    projects_file: Path = DEFAULT_PROJECTS_FILE
    state_file: Path = DEFAULT_STATE_FILE
    tmp_dir: Path = DEFAULT_TMP_DIR
    last_response_file: Path = DEFAULT_LAST_RESPONSE_FILE
    prompt_preamble: str = DEFAULT_PROMPT_PREAMBLE
    discovered_ollama_models: tuple[str, ...] = ()
    local_codex_profile: str = DEFAULT_LOCAL_CODEX_PROFILE

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        state_root = _path_env("STATE_ROOT", DEFAULT_STATE_ROOT)
        runtime_root = _path_env("RUNTIME_ROOT", DEFAULT_RUNTIME_ROOT)
        projects_file = _path_env("PROJECTS_FILE", state_root / "projects.json")
        discord_gateway_token = _first_present_env(
            "DISCORD_GATEWAY_TOKEN",
            "DISCORD_TOKEN",
        )
        control_guild_id = int(
            _first_present_env("CONTROL_GUILD_ID", "DISCORD_GUILD_ID")
        )
        control_channel_id = _int_env("CONTROL_CHANNEL_ID")
        allowed_user_ids = _csv_ints(_require_env("ALLOWED_USER_IDS"))
        codex_bin = os.environ.get("CODEX_BIN", "codex").strip() or "codex"
        codex_cwd = _path_env(
            "CODEX_CWD",
            Path("/home/boor123/work/codex_sandbox"),
        )
        codex_home_parent = (
            _path_env("CODEX_HOME_PARENT", runtime_root / "codex-home")
            if os.environ.get("CODEX_HOME_PARENT")
            else None
        )
        codex_home_seed_from = (
            _path_env("CODEX_HOME_SEED_FROM", Path.home() / ".codex")
            if os.environ.get("CODEX_HOME_SEED_FROM")
            else None
        )
        codex_status_home = _path_env("CODEX_STATUS_HOME", Path.home() / ".codex")
        prompt_max_chars = _int_env("PROMPT_MAX_CHARS", 4000)
        status_text_max_chars = _int_env("STATUS_TEXT_MAX_CHARS", 700)
        stream_tail_chars = _int_env(
            "STREAM_TAIL_CHARS",
            max(status_text_max_chars * 2, 2000),
        )
        stop_sigint_grace_seconds = _float_env("STOP_SIGINT_GRACE_SECONDS", 5.0)
        stop_sigterm_grace_seconds = _float_env("STOP_SIGTERM_GRACE_SECONDS", 5.0)
        response_preview_chars = _int_env("RESPONSE_PREVIEW_CHARS", 20)
        state_file = _path_env(
            "STATE_FILE",
            state_root / "gateway_state.json",
        )
        tmp_dir = _path_env("TMP_DIR", runtime_root / "tmp")
        last_response_file = _path_env(
            "LAST_RESPONSE_FILE",
            tmp_dir / "last_response.txt",
        )
        prompt_preamble = os.environ.get(
            "PROMPT_PREAMBLE",
            DEFAULT_PROMPT_PREAMBLE,
        ).strip()
        local_codex_profile = (
            os.environ.get("LOCAL_CODEX_PROFILE", DEFAULT_LOCAL_CODEX_PROFILE).strip()
            or DEFAULT_LOCAL_CODEX_PROFILE
        )
        discovered_ollama_models = discover_ollama_models()

        state_root.mkdir(parents=True, exist_ok=True)
        runtime_root.mkdir(parents=True, exist_ok=True)
        projects_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        last_response_file.parent.mkdir(parents=True, exist_ok=True)

        return cls(
            discord_gateway_token=discord_gateway_token,
            control_guild_id=control_guild_id,
            control_channel_id=control_channel_id,
            allowed_user_ids=allowed_user_ids,
            codex_bin=codex_bin,
            codex_cwd=codex_cwd,
            codex_home_parent=codex_home_parent,
            codex_home_seed_from=codex_home_seed_from,
            codex_status_home=codex_status_home,
            prompt_max_chars=prompt_max_chars,
            status_text_max_chars=status_text_max_chars,
            stream_tail_chars=stream_tail_chars,
            stop_sigint_grace_seconds=stop_sigint_grace_seconds,
            stop_sigterm_grace_seconds=stop_sigterm_grace_seconds,
            response_preview_chars=response_preview_chars,
            state_root=state_root,
            runtime_root=runtime_root,
            projects_file=projects_file,
            state_file=state_file,
            tmp_dir=tmp_dir,
            last_response_file=last_response_file,
            prompt_preamble=prompt_preamble,
            discovered_ollama_models=discovered_ollama_models,
            local_codex_profile=local_codex_profile,
        )
