from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..config import GatewayConfig
from ..execution_env import OPENAI_ENV
from ..state import GatewayState, LastRunSummary


@dataclass(frozen=True)
class StopResult:
    attempted: bool
    message: str


@dataclass(frozen=True)
class RunRequest:
    requester_user_id: int
    requester_name: str
    prompt: str
    project_id: str | None = None
    session_id: str | None = None
    session_ref: str | None = None
    start_new_session: bool = False
    discord_channel_name: str | None = None
    model_profile: str | None = None
    execution_env: str = OPENAI_ENV
    codex_home_path: Path | None = None
    runtime_root: Path | None = None
    last_response_file: Path | None = None
    project_label: str | None = None
    session_label: str | None = None


class Backend(ABC):
    name: str = ""

    @abstractmethod
    async def run(
        self,
        state: GatewayState,
        config: GatewayConfig,
        request: RunRequest,
        project_notification_sink: Callable[[str], None] | None = None,
    ) -> LastRunSummary: ...

    @abstractmethod
    async def stop(
        self,
        state: GatewayState,
        config: GatewayConfig,
    ) -> StopResult: ...


def get_backend(name: str = "codex") -> Backend:
    if name == "codex":
        from .codex import CodexBackend

        return CodexBackend()
    raise ValueError(f"Unknown backend: {name}")
