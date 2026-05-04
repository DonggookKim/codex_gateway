from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Callable

from .opencode import OpencodeBackend, OpencodeClient, PermissionAsk
from .opencode_server import OpencodeServer, OpencodeServerSettings

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class OpencodeRuntimeSettings:
    server: OpencodeServerSettings
    provider_id: str
    model_id: str
    default_agent: str = "build"
    idle_timeout_seconds: float = 7200.0


def opencode_runtime_settings_from_env(
    *,
    enabled_env: str = "OPENCODE_GATEWAY_ENABLED",
    binary_env: str = "OPENCODE_BIN",
    port_env: str = "OPENCODE_SERVER_PORT",
    hostname_env: str = "OPENCODE_SERVER_HOSTNAME",
    password_env: str = "OPENCODE_SERVER_PASSWORD",
    provider_env: str = "OPENCODE_PROVIDER_ID",
    model_env: str = "OPENCODE_MODEL_ID",
    agent_env: str = "OPENCODE_DEFAULT_AGENT",
    idle_timeout_env: str = "OPENCODE_IDLE_TIMEOUT_SECONDS",
) -> OpencodeRuntimeSettings | None:
    """Build runtime settings from env vars; returns None when disabled.

    Disabled means the gateway runs without an opencode runtime, so any
    session whose record has `backend == "opencode"` will fail at dispatch
    time with a clear error rather than silently fallback.
    """
    if os.environ.get(enabled_env, "").strip().lower() not in {"1", "true", "yes"}:
        return None

    provider_id = os.environ.get(provider_env, "").strip()
    model_id = os.environ.get(model_env, "").strip()
    if not provider_id or not model_id:
        LOGGER.warning(
            "Opencode runtime enabled but %s/%s not set; disabling",
            provider_env,
            model_env,
        )
        return None

    port_raw = os.environ.get(port_env, "").strip()
    try:
        port = int(port_raw) if port_raw else 14096
    except ValueError:
        LOGGER.warning("Invalid %s=%r; using default 14096", port_env, port_raw)
        port = 14096

    idle_raw = os.environ.get(idle_timeout_env, "").strip()
    try:
        idle_timeout = float(idle_raw) if idle_raw else 7200.0
    except ValueError:
        idle_timeout = 7200.0

    return OpencodeRuntimeSettings(
        server=OpencodeServerSettings(
            binary=os.environ.get(binary_env, "opencode").strip() or "opencode",
            port=port,
            hostname=os.environ.get(hostname_env, "127.0.0.1").strip() or "127.0.0.1",
            password=os.environ.get(password_env, "").strip() or None,
        ),
        provider_id=provider_id,
        model_id=model_id,
        default_agent=os.environ.get(agent_env, "build").strip() or "build",
        idle_timeout_seconds=idle_timeout,
    )


class OpencodeRuntime:
    """Lazily-started holder for the opencode server, client, and backend.

    Reused for the lifetime of the gateway process. `start()` is idempotent.
    The contained `OpencodeBackend` is shared across all opencode-backed
    sessions; it parses `provider/model` from `RunRequest.model_profile` so
    different sessions can use different models without rebuilding.
    """

    def __init__(
        self,
        settings: OpencodeRuntimeSettings,
        *,
        permission_callback: Callable[[PermissionAsk], None] | None = None,
    ) -> None:
        self.settings = settings
        self._permission_callback = permission_callback
        self._server: OpencodeServer | None = None
        self._client: OpencodeClient | None = None
        self._backend: OpencodeBackend | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> OpencodeBackend:
        async with self._lock:
            if self._backend is not None:
                return self._backend

            server = OpencodeServer(self.settings.server)
            url = await server.start()
            client = OpencodeClient(
                url,
                password=self.settings.server.password,
            )
            backend = OpencodeBackend(
                client=client,
                provider_id=self.settings.provider_id,
                model_id=self.settings.model_id,
                default_agent=self.settings.default_agent,
                idle_timeout_seconds=self.settings.idle_timeout_seconds,
                permission_callback=self._permission_callback,
            )
            self._server = server
            self._client = client
            self._backend = backend
            return backend

    @property
    def started(self) -> bool:
        return self._backend is not None

    @property
    def backend(self) -> OpencodeBackend:
        if self._backend is None:
            raise RuntimeError(
                "OpencodeRuntime is not started. Call await runtime.start() first."
            )
        return self._backend

    @property
    def client(self) -> OpencodeClient:
        if self._client is None:
            raise RuntimeError(
                "OpencodeRuntime is not started. Call await runtime.start() first."
            )
        return self._client

    @property
    def server_url(self) -> str | None:
        return self._server.url if self._server is not None else None

    def set_permission_callback(
        self,
        callback: Callable[[PermissionAsk], None] | None,
    ) -> None:
        self._permission_callback = callback
        if self._backend is not None:
            self._backend.permission_callback = callback

    async def stop(self) -> None:
        async with self._lock:
            if self._client is not None:
                try:
                    await self._client.close()
                except Exception:
                    LOGGER.exception("Error closing OpencodeClient")
                self._client = None
            if self._server is not None:
                try:
                    await self._server.stop()
                except Exception:
                    LOGGER.exception("Error stopping opencode serve")
                self._server = None
            self._backend = None
