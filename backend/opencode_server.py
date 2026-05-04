from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
from dataclasses import dataclass
from pathlib import Path

import aiohttp

LOGGER = logging.getLogger(__name__)


_LISTENING_RE = re.compile(
    r"opencode server listening on (http://[^\s]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class OpencodeServerSettings:
    binary: str = "opencode"
    port: int = 14096
    hostname: str = "127.0.0.1"
    password: str | None = None
    cwd: Path | None = None
    extra_args: tuple[str, ...] = ()
    startup_timeout_seconds: float = 30.0
    shutdown_timeout_seconds: float = 5.0


class OpencodeServer:
    """Manages an `opencode serve` child process.

    Behavior:
      - `start()` spawns a child if no opencode is already listening on the
        configured port; if one is already listening, attaches to the
        existing instance without spawning.
      - `stop()` only sends signals to processes this manager started.
      - `url` is None until the server is confirmed listening.
    """

    def __init__(self, settings: OpencodeServerSettings) -> None:
        self.settings = settings
        self._process: asyncio.subprocess.Process | None = None
        self._owns_process = False
        self._url: str | None = None
        self._stdout_reader_task: asyncio.Task | None = None
        self._stdout_lines: list[str] = []

    @property
    def url(self) -> str | None:
        return self._url

    @property
    def owns_process(self) -> bool:
        return self._owns_process

    @property
    def candidate_url(self) -> str:
        return f"http://{self.settings.hostname}:{self.settings.port}"

    async def start(self) -> str:
        if await self._probe(self.candidate_url):
            self._url = self.candidate_url
            self._owns_process = False
            LOGGER.info(
                "Reusing pre-existing opencode server at %s",
                self._url,
            )
            return self._url

        argv = [
            self.settings.binary,
            "serve",
            "--port",
            str(self.settings.port),
            "--hostname",
            self.settings.hostname,
        ]
        argv.extend(self.settings.extra_args)

        env = dict(os.environ)
        if self.settings.password:
            env["OPENCODE_SERVER_PASSWORD"] = self.settings.password

        self._process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(self.settings.cwd) if self.settings.cwd is not None else None,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        self._owns_process = True
        self._stdout_reader_task = asyncio.create_task(self._drain_stdout())

        try:
            self._url = await asyncio.wait_for(
                self._await_listening(),
                timeout=self.settings.startup_timeout_seconds,
            )
        except asyncio.TimeoutError:
            await self.stop()
            raise RuntimeError(
                f"opencode serve did not report listening on {self.candidate_url} "
                f"within {self.settings.startup_timeout_seconds}s. "
                f"Last output: {''.join(self._stdout_lines[-10:])!r}"
            )

        return self._url

    async def stop(self) -> None:
        process = self._process
        if process is None or not self._owns_process:
            self._url = None
            return

        if process.returncode is None:
            try:
                process.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=self.settings.shutdown_timeout_seconds,
                )
            except asyncio.TimeoutError:
                if process.returncode is None:
                    process.kill()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        LOGGER.warning(
                            "opencode serve PID %s did not exit after SIGKILL",
                            process.pid,
                        )

        if self._stdout_reader_task is not None and not self._stdout_reader_task.done():
            self._stdout_reader_task.cancel()
            try:
                await self._stdout_reader_task
            except (asyncio.CancelledError, Exception):
                pass

        self._process = None
        self._owns_process = False
        self._url = None

    async def _await_listening(self) -> str:
        # Race two signals: stdout banner OR /event handshake responding.
        while True:
            for line in self._stdout_lines:
                match = _LISTENING_RE.search(line)
                if match:
                    url = match.group(1).rstrip("/")
                    return url
            if await self._probe(self.candidate_url):
                return self.candidate_url
            if self._process is not None and self._process.returncode is not None:
                raise RuntimeError(
                    f"opencode serve exited prematurely with code "
                    f"{self._process.returncode}: "
                    f"{''.join(self._stdout_lines[-10:])!r}"
                )
            await asyncio.sleep(0.2)

    async def _drain_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            async for raw in process.stdout:
                line = raw.decode("utf-8", errors="replace")
                self._stdout_lines.append(line)
                # Keep the buffer bounded so a long-running serve doesn't
                # leak memory through this manager.
                if len(self._stdout_lines) > 500:
                    del self._stdout_lines[:250]
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("opencode serve stdout reader errored")

    async def _probe(self, base_url: str) -> bool:
        try:
            timeout = aiohttp.ClientTimeout(total=1.5)
            auth = (
                aiohttp.BasicAuth("opencode", self.settings.password)
                if self.settings.password
                else None
            )
            async with aiohttp.ClientSession(
                timeout=timeout, auth=auth
            ) as session:
                async with session.get(f"{base_url}/event") as resp:
                    if resp.status >= 400:
                        return False
                    # Read just the first SSE chunk to confirm liveness, then drop.
                    async for _ in resp.content.iter_any():
                        return True
                    return False
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return False
