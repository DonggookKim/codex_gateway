from __future__ import annotations

from typing import Callable

from ..config import GatewayConfig
from ..state import GatewayState, LastRunSummary
from . import Backend, RunRequest, StopResult


class CodexBackend(Backend):
    name = "codex"

    async def run(
        self,
        state: GatewayState,
        config: GatewayConfig,
        request: RunRequest,
        project_notification_sink: Callable[[str], None] | None = None,
    ) -> LastRunSummary:
        from ..runner import run_codex

        return await run_codex(
            state=state,
            config=config,
            requester_user_id=request.requester_user_id,
            requester_name=request.requester_name,
            prompt=request.prompt,
            project_id=request.project_id,
            session_id=request.session_id,
            session_ref=request.session_ref,
            start_new_session=request.start_new_session,
            discord_channel_name=request.discord_channel_name,
            model_profile=request.model_profile,
            execution_env=request.execution_env,
            codex_home_path=request.codex_home_path,
            runtime_root=request.runtime_root,
            last_response_file=request.last_response_file,
            project_label=request.project_label,
            session_label=request.session_label,
            project_notification_sink=project_notification_sink,
        )

    async def stop(
        self,
        state: GatewayState,
        config: GatewayConfig,
    ) -> StopResult:
        from ..runner import stop_active_run

        return await stop_active_run(state, config)
