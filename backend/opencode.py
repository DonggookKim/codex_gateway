from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Union

import aiohttp

from ..config import GatewayConfig
from ..formatter import excerpt
from ..state import GatewayState, LastRunSummary, utc_now
from . import Backend, RunRequest, StopResult


PermissionCallback = Callable[["PermissionAsk"], Union[None, Awaitable[None]]]

LOGGER = logging.getLogger(__name__)


class OpencodeAPIError(RuntimeError):
    def __init__(self, status: int, body: str, *, path: str = "") -> None:
        super().__init__(f"opencode API error {status} on {path}: {body[:200]}")
        self.status = status
        self.body = body
        self.path = path


@dataclass
class PermissionAsk:
    permission_id: str
    session_id: str
    permission: str
    patterns: list[str]
    always: list[str]
    tool_message_id: str | None
    tool_call_id: str | None
    metadata: dict[str, object]
    asked_at: float = field(default_factory=time.time)


class OpencodeClient:
    """Thin async wrapper around the opencode serve HTTP + SSE API."""

    def __init__(
        self,
        server_url: str,
        *,
        password: str | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self._password = password
        self._owns_session = session is None
        self._session = session

    async def __aenter__(self) -> "OpencodeClient":
        await self._ensure_session()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            auth = (
                aiohttp.BasicAuth("opencode", self._password)
                if self._password
                else None
            )
            self._session = aiohttp.ClientSession(auth=auth)
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.server_url}{path}"

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
    ) -> dict | list | bool | None:
        session = await self._ensure_session()
        async with session.request(method, self._url(path), json=json_body) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise OpencodeAPIError(resp.status, text, path=path)
            if not text:
                return None
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text

    async def create_session(
        self,
        *,
        title: str | None = None,
        parent: str | None = None,
    ) -> dict:
        body: dict[str, object] = {}
        if title:
            body["title"] = title
        if parent:
            body["parentID"] = parent
        result = await self._request_json("POST", "/session", json_body=body)
        if not isinstance(result, dict):
            raise OpencodeAPIError(0, str(result), path="/session")
        return result

    async def delete_session(self, session_id: str) -> bool:
        result = await self._request_json("DELETE", f"/session/{session_id}")
        return bool(result)

    async def list_sessions(self) -> list[dict]:
        result = await self._request_json("GET", "/session")
        if not isinstance(result, list):
            return []
        return [item for item in result if isinstance(item, dict)]

    async def get_session(self, session_id: str) -> dict | None:
        try:
            result = await self._request_json("GET", f"/session/{session_id}")
        except OpencodeAPIError as exc:
            if exc.status == 404:
                return None
            raise
        return result if isinstance(result, dict) else None

    async def send_prompt_async(
        self,
        session_id: str,
        prompt: str,
        *,
        provider_id: str,
        model_id: str,
        agent: str = "build",
    ) -> dict:
        body = {
            "providerID": provider_id,
            "modelID": model_id,
            "agent": agent,
            "parts": [{"type": "text", "text": prompt}],
        }
        result = await self._request_json(
            "POST",
            f"/session/{session_id}/prompt_async",
            json_body=body,
        )
        return result if isinstance(result, dict) else {}

    async def abort_session(self, session_id: str) -> bool:
        try:
            result = await self._request_json(
                "POST",
                f"/session/{session_id}/abort",
            )
        except OpencodeAPIError as exc:
            if exc.status == 404:
                return False
            raise
        return bool(result)

    async def reply_permission(
        self,
        session_id: str,
        permission_id: str,
        response: str,
        *,
        remember: bool = False,
    ) -> bool:
        if response not in {"once", "always", "reject"}:
            raise ValueError(
                f"permission response must be one of once|always|reject, got {response!r}"
            )
        body: dict[str, object] = {"response": response}
        if remember:
            body["remember"] = True
        result = await self._request_json(
            "POST",
            f"/session/{session_id}/permissions/{permission_id}",
            json_body=body,
        )
        return bool(result)

    async def stream_events(self) -> AsyncIterator[dict]:
        """Yield decoded SSE events from /event indefinitely until cancelled."""
        session = await self._ensure_session()
        async with session.get(self._url("/event")) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise OpencodeAPIError(resp.status, text, path="/event")
            buffer = ""
            async for raw_chunk in resp.content.iter_any():
                buffer += raw_chunk.decode("utf-8", errors="replace")
                while "\n\n" in buffer:
                    block, buffer = buffer.split("\n\n", 1)
                    payload = self._parse_sse_block(block)
                    if payload is not None:
                        yield payload

    @staticmethod
    def _parse_sse_block(block: str) -> dict | None:
        data_lines: list[str] = []
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            return None
        data = "\n".join(data_lines)
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return None


def parse_permission_asked(event: dict) -> PermissionAsk | None:
    if event.get("type") != "permission.asked":
        return None
    props = event.get("properties") or {}
    permission_id = props.get("id")
    session_id = props.get("sessionID")
    if not isinstance(permission_id, str) or not isinstance(session_id, str):
        return None
    tool = props.get("tool") or {}
    return PermissionAsk(
        permission_id=permission_id,
        session_id=session_id,
        permission=str(props.get("permission", "")),
        patterns=[str(p) for p in props.get("patterns", []) if p is not None],
        always=[str(p) for p in props.get("always", []) if p is not None],
        tool_message_id=(
            str(tool.get("messageID")) if isinstance(tool, dict) and tool.get("messageID") else None
        ),
        tool_call_id=(
            str(tool.get("callID")) if isinstance(tool, dict) and tool.get("callID") else None
        ),
        metadata=dict(props.get("metadata") or {}),
    )


@dataclass
class _RunCollector:
    """Aggregates SSE events for a single run() invocation."""

    target_session_id: str
    text_chunks: list[str] = field(default_factory=list)
    tool_errors: list[str] = field(default_factory=list)
    permission_asks: list[PermissionAsk] = field(default_factory=list)
    finish_reason: str | None = None
    completed: bool = False

    def feed(self, event: dict) -> None:
        if not isinstance(event, dict):
            return
        event_type = event.get("type")
        props = event.get("properties") or {}
        if props.get("sessionID") not in {None, self.target_session_id}:
            return

        if event_type == "message.part.delta":
            if props.get("field") == "text":
                delta = props.get("delta")
                if isinstance(delta, str):
                    self.text_chunks.append(delta)
            return

        if event_type == "message.part.updated":
            part = props.get("part") or {}
            if not isinstance(part, dict):
                return
            if part.get("type") == "tool":
                state = part.get("state") or {}
                if isinstance(state, dict) and state.get("status") == "error":
                    err = state.get("error")
                    if isinstance(err, str):
                        self.tool_errors.append(err)
            elif part.get("type") == "step-finish":
                reason = part.get("reason")
                if isinstance(reason, str):
                    self.finish_reason = reason
            return

        if event_type == "permission.asked":
            ask = parse_permission_asked(event)
            if ask is not None and ask.session_id == self.target_session_id:
                self.permission_asks.append(ask)
            return

        if event_type == "session.idle":
            if props.get("sessionID") == self.target_session_id:
                self.completed = True
            return

    @property
    def assistant_text(self) -> str:
        return "".join(self.text_chunks).strip()


class OpencodeBackend(Backend):
    """Backend that drives an opencode serve instance over HTTP + SSE.

    The backend does not auto-respond to permission asks; it surfaces them
    via `permission_callback` so the gateway can route them to a Discord
    operator. The run() call still blocks until the opencode session reaches
    `idle`, which happens once the operator approves or rejects.
    """

    name = "opencode"

    def __init__(
        self,
        client: OpencodeClient,
        *,
        provider_id: str,
        model_id: str,
        default_agent: str = "build",
        permission_callback: PermissionCallback | None = None,
        idle_timeout_seconds: float = 7200.0,
    ) -> None:
        self.client = client
        self.provider_id = provider_id
        self.model_id = model_id
        self.default_agent = default_agent
        self.permission_callback = permission_callback
        self.idle_timeout_seconds = idle_timeout_seconds

    def _resolve_provider_model(self, request: RunRequest) -> tuple[str, str]:
        profile = (request.model_profile or "").strip()
        if "/" in profile:
            provider, _, model = profile.partition("/")
            if provider and model:
                return provider, model
        return self.provider_id, self.model_id

    async def run(
        self,
        state: GatewayState,
        config: GatewayConfig,
        request: RunRequest,
        project_notification_sink: Callable[[str], None] | None = None,
    ) -> LastRunSummary:
        started_at = utc_now()
        prompt_excerpt = excerpt(request.prompt, config.status_text_max_chars)
        provider_id, model_id = self._resolve_provider_model(request)

        target_session_id = request.session_ref
        if not target_session_id or request.start_new_session:
            session_info = await self.client.create_session(
                title=request.session_label or None,
            )
            target_session_id = str(session_info.get("id") or "")
            if not target_session_id:
                raise OpencodeAPIError(0, "no session id returned", path="/session")

        collector = _RunCollector(target_session_id=target_session_id)

        async def _consume_events() -> None:
            try:
                async for event in self.client.stream_events():
                    collector.feed(event)
                    if (
                        event.get("type") == "permission.asked"
                        and self.permission_callback is not None
                    ):
                        ask = parse_permission_asked(event)
                        if ask is not None and ask.session_id == target_session_id:
                            try:
                                result = self.permission_callback(ask)
                                if inspect.isawaitable(result):
                                    await result
                            except Exception:
                                LOGGER.exception(
                                    "permission_callback raised for %s",
                                    ask.permission_id,
                                )
                    if collector.completed:
                        return
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("opencode SSE stream errored")

        events_task = asyncio.create_task(_consume_events())
        timed_out = False

        try:
            await self.client.send_prompt_async(
                target_session_id,
                request.prompt,
                provider_id=provider_id,
                model_id=model_id,
                agent=self.default_agent,
            )
            try:
                await asyncio.wait_for(events_task, timeout=self.idle_timeout_seconds)
            except asyncio.TimeoutError:
                # β behavior: abort the session so the model run terminates,
                # surface a TIMEOUT signal in the summary, and let the caller
                # notify the operator. We do NOT auto-respond to permissions.
                timed_out = True
                try:
                    await self.client.abort_session(target_session_id)
                except Exception:
                    LOGGER.exception(
                        "abort_session failed after idle timeout for %s",
                        target_session_id,
                    )
        finally:
            if not events_task.done():
                events_task.cancel()
                try:
                    await events_task
                except (asyncio.CancelledError, Exception):
                    pass

        finished_at = utc_now()
        if timed_out:
            exit_code = None
            exit_signal = "TIMEOUT"
        else:
            exit_code = 0 if collector.finish_reason == "stop" else None
            exit_signal = (
                None
                if collector.finish_reason == "stop"
                else (collector.finish_reason or "INCOMPLETE").upper()
            )

        # Persist the full assistant text to the per-session last-response
        # artifact so `/last` can attach it. The codex backend gets this for
        # free via `codex exec -o <file>`; the opencode REST/SSE path has to
        # do it explicitly. LastRunSummary only carries a truncated excerpt,
        # which is not what `/last` is meant to surface.
        if request.last_response_file is not None and collector.assistant_text.strip():
            from ..storage.last_response_store import write_last_response_text

            try:
                write_last_response_text(
                    request.last_response_file,
                    collector.assistant_text,
                )
            except OSError:
                LOGGER.exception(
                    "Failed to write opencode last_response to %s",
                    request.last_response_file,
                )

        return LastRunSummary(
            run_id=target_session_id[:8],
            requester_user_id=request.requester_user_id,
            requester_name=request.requester_name,
            prompt_excerpt=prompt_excerpt,
            started_at=started_at,
            finished_at=finished_at,
            exit_code=exit_code,
            exit_signal=exit_signal,
            stdout_excerpt=excerpt(collector.assistant_text, config.status_text_max_chars),
            stderr_excerpt=excerpt(
                "\n".join(collector.tool_errors),
                config.status_text_max_chars,
            ),
            assistant_response_excerpt=excerpt(
                collector.assistant_text,
                config.status_text_max_chars,
            ),
            codex_thread_ref=target_session_id,
        )

    async def stop(
        self,
        state: GatewayState,
        config: GatewayConfig,
    ) -> StopResult:
        active_run = state.active_run
        session_id = (
            active_run.session_id
            if active_run is not None and active_run.session_id
            else state.selection_state.get("selected_session_id", "")
        )
        if not session_id:
            return StopResult(
                attempted=False,
                message="No active opencode session to abort.",
            )
        aborted = await self.client.abort_session(session_id)
        return StopResult(
            attempted=True,
            message=(
                f"opencode session `{session_id}` abort {'sent' if aborted else 'failed'}."
            ),
        )
