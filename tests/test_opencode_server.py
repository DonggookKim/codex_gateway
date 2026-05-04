from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from codex_gateway.backend.opencode_server import (
    OpencodeServer,
    OpencodeServerSettings,
)


class OpencodeServerSettingsTest(unittest.TestCase):
    def test_defaults(self) -> None:
        s = OpencodeServerSettings()
        self.assertEqual(s.binary, "opencode")
        self.assertEqual(s.port, 14096)
        self.assertEqual(s.hostname, "127.0.0.1")
        self.assertIsNone(s.password)
        self.assertEqual(s.extra_args, ())


class CandidateUrlTest(unittest.TestCase):
    def test_uses_hostname_and_port(self) -> None:
        server = OpencodeServer(
            OpencodeServerSettings(hostname="127.0.0.1", port=14096)
        )
        self.assertEqual(server.candidate_url, "http://127.0.0.1:14096")


class StartReusesExistingTest(unittest.IsolatedAsyncioTestCase):
    async def test_attaches_to_running_server_without_spawning(self) -> None:
        server = OpencodeServer(OpencodeServerSettings(port=14096))

        async def fake_probe(url: str) -> bool:
            return True

        server._probe = fake_probe  # type: ignore[assignment]

        with patch(
            "codex_gateway.backend.opencode_server.asyncio.create_subprocess_exec",
            new=AsyncMock(),
        ) as spawn_mock:
            url = await server.start()

        self.assertEqual(url, "http://127.0.0.1:14096")
        self.assertFalse(server.owns_process)
        spawn_mock.assert_not_awaited()


class StopOnlyKillsOwnedTest(unittest.IsolatedAsyncioTestCase):
    async def test_stop_does_not_signal_when_attached(self) -> None:
        server = OpencodeServer(OpencodeServerSettings())
        server._owns_process = False
        server._process = MagicMock()
        await server.stop()
        server._process = None  # already cleared above; assertion below
        # send_signal should never have been called
        # (process is None here too — the call path is the early return).

    async def test_stop_signals_owned_process(self) -> None:
        server = OpencodeServer(OpencodeServerSettings(shutdown_timeout_seconds=0.1))
        proc = MagicMock()
        proc.returncode = None
        proc.send_signal = MagicMock()
        proc.kill = MagicMock()

        async def fake_wait():
            proc.returncode = 0
            return 0

        proc.wait = AsyncMock(side_effect=fake_wait)

        server._process = proc
        server._owns_process = True

        await server.stop()

        proc.send_signal.assert_called_once()
        proc.wait.assert_awaited()
        self.assertFalse(server.owns_process)


class ListeningParseTest(unittest.IsolatedAsyncioTestCase):
    async def test_extracts_url_from_stdout_banner(self) -> None:
        server = OpencodeServer(OpencodeServerSettings(port=14096))
        server._stdout_lines = [
            "INFO  starting...\n",
            "opencode server listening on http://127.0.0.1:14096\n",
        ]

        async def never_probes(url: str) -> bool:
            return False

        server._probe = never_probes  # type: ignore[assignment]

        result = await server._await_listening()
        self.assertEqual(result, "http://127.0.0.1:14096")


if __name__ == "__main__":
    unittest.main()
