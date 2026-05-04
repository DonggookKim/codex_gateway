from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from codex_gateway.backend import Backend, RunRequest, StopResult, get_backend
from codex_gateway.backend.codex import CodexBackend


class GetBackendTest(unittest.TestCase):
    def test_default_is_codex(self) -> None:
        backend = get_backend()
        self.assertIsInstance(backend, CodexBackend)
        self.assertEqual(backend.name, "codex")

    def test_explicit_codex(self) -> None:
        backend = get_backend("codex")
        self.assertIsInstance(backend, CodexBackend)

    def test_unknown_backend_raises(self) -> None:
        with self.assertRaises(ValueError):
            get_backend("does-not-exist")

    def test_codex_backend_implements_abc(self) -> None:
        self.assertTrue(issubclass(CodexBackend, Backend))


class CodexBackendDelegationTest(unittest.IsolatedAsyncioTestCase):
    async def test_run_delegates_to_run_codex(self) -> None:
        backend = CodexBackend()
        state = MagicMock(name="state")
        config = MagicMock(name="config")
        request = RunRequest(
            requester_user_id=42,
            requester_name="alice",
            prompt="hello",
            project_id="p1",
            session_id="s1",
            session_ref="ref-1",
            start_new_session=False,
            model_profile="gpt-5.2",
        )
        sink = MagicMock(name="sink")
        run_codex_mock = AsyncMock(return_value="summary-sentinel")

        with patch("codex_gateway.runner.run_codex", new=run_codex_mock):
            result = await backend.run(state, config, request, sink)

        self.assertEqual(result, "summary-sentinel")
        kwargs = run_codex_mock.await_args.kwargs
        self.assertIs(kwargs["state"], state)
        self.assertIs(kwargs["config"], config)
        self.assertEqual(kwargs["requester_user_id"], 42)
        self.assertEqual(kwargs["prompt"], "hello")
        self.assertEqual(kwargs["session_ref"], "ref-1")
        self.assertEqual(kwargs["model_profile"], "gpt-5.2")
        self.assertIs(kwargs["project_notification_sink"], sink)

    async def test_stop_delegates_to_stop_active_run(self) -> None:
        backend = CodexBackend()
        state = MagicMock(name="state")
        config = MagicMock(name="config")
        expected = StopResult(attempted=True, message="stopped")
        stop_mock = AsyncMock(return_value=expected)

        with patch("codex_gateway.runner.stop_active_run", new=stop_mock):
            result = await backend.stop(state, config)

        self.assertEqual(result, expected)
        stop_mock.assert_awaited_once_with(state, config)


if __name__ == "__main__":
    unittest.main()
