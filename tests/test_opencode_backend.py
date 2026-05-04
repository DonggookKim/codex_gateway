from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from codex_gateway.backend import RunRequest
from codex_gateway.backend.opencode import (
    OpencodeBackend,
    OpencodeClient,
    PermissionAsk,
    _RunCollector,
    parse_permission_asked,
)


class ParsePermissionAskedTest(unittest.TestCase):
    def test_returns_none_for_non_permission_event(self) -> None:
        self.assertIsNone(parse_permission_asked({"type": "session.idle"}))

    def test_extracts_fields(self) -> None:
        event = {
            "type": "permission.asked",
            "properties": {
                "id": "per_abc",
                "sessionID": "ses_xyz",
                "permission": "bash",
                "patterns": ["date"],
                "always": ["date *"],
                "metadata": {"foo": "bar"},
                "tool": {"messageID": "msg_1", "callID": "call_1"},
            },
        }
        ask = parse_permission_asked(event)
        assert ask is not None
        self.assertEqual(ask.permission_id, "per_abc")
        self.assertEqual(ask.session_id, "ses_xyz")
        self.assertEqual(ask.permission, "bash")
        self.assertEqual(ask.patterns, ["date"])
        self.assertEqual(ask.always, ["date *"])
        self.assertEqual(ask.metadata, {"foo": "bar"})
        self.assertEqual(ask.tool_message_id, "msg_1")
        self.assertEqual(ask.tool_call_id, "call_1")

    def test_returns_none_if_id_missing(self) -> None:
        event = {
            "type": "permission.asked",
            "properties": {"sessionID": "ses_xyz"},
        }
        self.assertIsNone(parse_permission_asked(event))


class RunCollectorTest(unittest.TestCase):
    def test_accumulates_text_deltas(self) -> None:
        c = _RunCollector(target_session_id="s1")
        c.feed({
            "type": "message.part.delta",
            "properties": {"sessionID": "s1", "field": "text", "delta": "Hello "},
        })
        c.feed({
            "type": "message.part.delta",
            "properties": {"sessionID": "s1", "field": "text", "delta": "world"},
        })
        self.assertEqual(c.assistant_text, "Hello world")

    def test_ignores_other_sessions(self) -> None:
        c = _RunCollector(target_session_id="s1")
        c.feed({
            "type": "message.part.delta",
            "properties": {"sessionID": "s2", "field": "text", "delta": "nope"},
        })
        self.assertEqual(c.assistant_text, "")

    def test_records_tool_errors(self) -> None:
        c = _RunCollector(target_session_id="s1")
        c.feed({
            "type": "message.part.updated",
            "properties": {
                "sessionID": "s1",
                "part": {
                    "type": "tool",
                    "state": {"status": "error", "error": "permission rejected"},
                },
            },
        })
        self.assertEqual(c.tool_errors, ["permission rejected"])

    def test_finish_reason_from_step_finish(self) -> None:
        c = _RunCollector(target_session_id="s1")
        c.feed({
            "type": "message.part.updated",
            "properties": {
                "sessionID": "s1",
                "part": {"type": "step-finish", "reason": "stop"},
            },
        })
        self.assertEqual(c.finish_reason, "stop")

    def test_session_idle_marks_complete(self) -> None:
        c = _RunCollector(target_session_id="s1")
        self.assertFalse(c.completed)
        c.feed({"type": "session.idle", "properties": {"sessionID": "s1"}})
        self.assertTrue(c.completed)

    def test_session_idle_for_other_session_ignored(self) -> None:
        c = _RunCollector(target_session_id="s1")
        c.feed({"type": "session.idle", "properties": {"sessionID": "other"}})
        self.assertFalse(c.completed)

    def test_records_permission_asks(self) -> None:
        c = _RunCollector(target_session_id="s1")
        c.feed({
            "type": "permission.asked",
            "properties": {
                "id": "per_1",
                "sessionID": "s1",
                "permission": "bash",
                "patterns": ["ls"],
                "always": ["ls *"],
                "tool": {"messageID": "m", "callID": "c"},
            },
        })
        self.assertEqual(len(c.permission_asks), 1)
        self.assertEqual(c.permission_asks[0].permission_id, "per_1")


class OpencodeClientSseParseTest(unittest.TestCase):
    def test_parses_sse_block_with_data_prefix(self) -> None:
        block = 'data: {"type":"server.connected","properties":{}}'
        parsed = OpencodeClient._parse_sse_block(block)
        self.assertEqual(parsed, {"type": "server.connected", "properties": {}})

    def test_returns_none_for_comment_only_block(self) -> None:
        self.assertIsNone(OpencodeClient._parse_sse_block(": keepalive"))

    def test_returns_none_for_invalid_json(self) -> None:
        self.assertIsNone(OpencodeClient._parse_sse_block("data: not-json"))


class OpencodeClientUrlTest(unittest.TestCase):
    def test_strips_trailing_slash(self) -> None:
        c = OpencodeClient("http://localhost:4096/")
        self.assertEqual(c.server_url, "http://localhost:4096")

    def test_url_prepends_slash(self) -> None:
        c = OpencodeClient("http://localhost:4096")
        self.assertEqual(c._url("session"), "http://localhost:4096/session")
        self.assertEqual(c._url("/session"), "http://localhost:4096/session")


class ReplyPermissionValidationTest(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_invalid_response_value(self) -> None:
        c = OpencodeClient("http://localhost")
        with self.assertRaises(ValueError):
            await c.reply_permission("s1", "p1", "approve")


class OpencodeBackendRunTest(unittest.IsolatedAsyncioTestCase):
    async def test_run_creates_session_when_no_session_ref(self) -> None:
        client = MagicMock(spec=OpencodeClient)
        client.create_session = AsyncMock(return_value={"id": "ses_new"})
        client.send_prompt_async = AsyncMock(return_value={})

        async def _stream():
            for ev in [
                {"type": "message.part.delta", "properties": {"sessionID": "ses_new", "field": "text", "delta": "hi"}},
                {"type": "message.part.updated", "properties": {"sessionID": "ses_new", "part": {"type": "step-finish", "reason": "stop"}}},
                {"type": "session.idle", "properties": {"sessionID": "ses_new"}},
            ]:
                yield ev

        client.stream_events = lambda: _stream()

        backend = OpencodeBackend(
            client=client,
            provider_id="model-connect",
            model_id="Qwen3.5-397B-A17B-FP8",
        )

        config = MagicMock()
        config.status_text_max_chars = 200
        state = MagicMock()
        request = RunRequest(
            requester_user_id=1,
            requester_name="op",
            prompt="hello",
            start_new_session=True,
        )

        summary = await backend.run(state, config, request)

        client.create_session.assert_awaited_once()
        client.send_prompt_async.assert_awaited_once()
        kwargs = client.send_prompt_async.await_args.kwargs
        # send_prompt_async was called as positional + kwargs; verify provider/model
        args = client.send_prompt_async.await_args.args
        self.assertEqual(args[0], "ses_new")
        self.assertEqual(args[1], "hello")
        self.assertEqual(kwargs["provider_id"], "model-connect")
        self.assertEqual(kwargs["model_id"], "Qwen3.5-397B-A17B-FP8")
        self.assertEqual(summary.exit_code, 0)
        self.assertEqual(summary.codex_thread_ref, "ses_new")
        self.assertEqual(summary.assistant_response_excerpt, "hi")

    async def test_run_invokes_permission_callback(self) -> None:
        client = MagicMock(spec=OpencodeClient)
        client.send_prompt_async = AsyncMock(return_value={})
        client.create_session = AsyncMock(return_value={"id": "ses_x"})

        async def _stream():
            for ev in [
                {
                    "type": "permission.asked",
                    "properties": {
                        "id": "per_1",
                        "sessionID": "ses_x",
                        "permission": "bash",
                        "patterns": ["date"],
                        "always": ["date *"],
                        "tool": {"messageID": "m", "callID": "c"},
                    },
                },
                {"type": "message.part.updated", "properties": {"sessionID": "ses_x", "part": {"type": "step-finish", "reason": "stop"}}},
                {"type": "session.idle", "properties": {"sessionID": "ses_x"}},
            ]:
                yield ev

        client.stream_events = lambda: _stream()

        seen: list[PermissionAsk] = []
        backend = OpencodeBackend(
            client=client,
            provider_id="p",
            model_id="m",
            permission_callback=seen.append,
        )

        config = MagicMock()
        config.status_text_max_chars = 200
        request = RunRequest(
            requester_user_id=1,
            requester_name="op",
            prompt="hello",
            session_ref="ses_x",
        )

        await backend.run(MagicMock(), config, request)

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].permission_id, "per_1")
        self.assertEqual(seen[0].session_id, "ses_x")

    async def test_run_records_tool_error(self) -> None:
        client = MagicMock(spec=OpencodeClient)
        client.send_prompt_async = AsyncMock(return_value={})
        client.create_session = AsyncMock(return_value={"id": "ses_x"})

        async def _stream():
            for ev in [
                {
                    "type": "message.part.updated",
                    "properties": {
                        "sessionID": "ses_x",
                        "part": {
                            "type": "tool",
                            "state": {
                                "status": "error",
                                "error": "The user rejected permission",
                            },
                        },
                    },
                },
                {
                    "type": "message.part.updated",
                    "properties": {"sessionID": "ses_x", "part": {"type": "step-finish", "reason": "tool-calls"}},
                },
                {"type": "session.idle", "properties": {"sessionID": "ses_x"}},
            ]:
                yield ev

        client.stream_events = lambda: _stream()

        backend = OpencodeBackend(client=client, provider_id="p", model_id="m")
        config = MagicMock()
        config.status_text_max_chars = 200
        request = RunRequest(
            requester_user_id=1,
            requester_name="op",
            prompt="x",
            session_ref="ses_x",
        )

        summary = await backend.run(MagicMock(), config, request)
        self.assertIsNone(summary.exit_code)
        self.assertEqual(summary.exit_signal, "TOOL-CALLS")
        self.assertIn("rejected permission", summary.stderr_excerpt)


class OpencodeBackendStopTest(unittest.IsolatedAsyncioTestCase):
    async def test_stop_aborts_active_session(self) -> None:
        client = MagicMock(spec=OpencodeClient)
        client.abort_session = AsyncMock(return_value=True)
        backend = OpencodeBackend(client=client, provider_id="p", model_id="m")

        active_run = MagicMock()
        active_run.session_id = "ses_active"
        state = MagicMock()
        state.active_run = active_run
        state.selection_state = {}

        result = await backend.stop(state, MagicMock())

        client.abort_session.assert_awaited_once_with("ses_active")
        self.assertTrue(result.attempted)
        self.assertIn("ses_active", result.message)

    async def test_stop_falls_back_to_selection(self) -> None:
        client = MagicMock(spec=OpencodeClient)
        client.abort_session = AsyncMock(return_value=True)
        backend = OpencodeBackend(client=client, provider_id="p", model_id="m")

        state = MagicMock()
        state.active_run = None
        state.selection_state = {"selected_session_id": "ses_fallback"}

        result = await backend.stop(state, MagicMock())
        client.abort_session.assert_awaited_once_with("ses_fallback")
        self.assertTrue(result.attempted)

    async def test_stop_when_nothing_active(self) -> None:
        client = MagicMock(spec=OpencodeClient)
        client.abort_session = AsyncMock()
        backend = OpencodeBackend(client=client, provider_id="p", model_id="m")

        state = MagicMock()
        state.active_run = None
        state.selection_state = {}

        result = await backend.stop(state, MagicMock())
        self.assertFalse(result.attempted)
        client.abort_session.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
