from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import asyncio
import tempfile
import unittest

from codex_gateway.formatter import format_run_complete, format_status
from codex_gateway.session_inspector import LatestCodexResponse
from codex_gateway.state import GatewayState, LastRunSummary


class FormatterPreviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

    def tearDown(self) -> None:
        self._loop.close()
        asyncio.set_event_loop(None)

    def test_format_run_complete_uses_short_response_preview(self) -> None:
        summary = LastRunSummary(
            run_id="abc12345",
            requester_user_id=1,
            requester_name="tester",
            prompt_excerpt="long prompt",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            exit_code=0,
            exit_signal=None,
            stdout_excerpt="",
            stderr_excerpt="",
            assistant_response_excerpt="01234567890123456789ABCDEFGHIJ",
        )

        message = format_run_complete(summary, text_limit=700, response_preview_chars=20)

        self.assertIn("Last response: 01234567890123456789…", message)
        self.assertNotIn("01234567890123456789ABCDEFGHIJ", message)

    def test_format_status_uses_short_response_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = GatewayState(Path(temp_dir) / "gateway_state.json")
            state.finish_run(
                LastRunSummary(
                    run_id="run12345",
                    requester_user_id=1,
                    requester_name="tester",
                    prompt_excerpt="prompt",
                    started_at=datetime.now(timezone.utc),
                    finished_at=datetime.now(timezone.utc),
                    exit_code=0,
                    exit_signal=None,
                    stdout_excerpt="",
                    stderr_excerpt="",
                    assistant_response_excerpt="stale gateway response",
                )
            )

            latest = LatestCodexResponse(
                session_id="session-1",
                timestamp="2026-04-21T00:00:00Z",
                text="01234567890123456789ABCDEFGHIJ",
            )

            message = format_status(
                state,
                text_limit=700,
                latest_response=latest,
                response_preview_chars=20,
            )

        self.assertIn("Last Codex response:", message)
        self.assertIn("Last Codex response: 01234567890123456789…", message)
        self.assertNotIn(latest.text, message)

    def test_format_status_reports_external_codex_activity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = GatewayState(Path(temp_dir) / "gateway_state.json")
            state.finish_run(
                LastRunSummary(
                    run_id="run12345",
                    requester_user_id=1,
                    requester_name="tester",
                    prompt_excerpt="prompt",
                    started_at=datetime.now(timezone.utc),
                    finished_at=datetime.now(timezone.utc),
                    exit_code=0,
                    exit_signal=None,
                    stdout_excerpt="",
                    stderr_excerpt="",
                    assistant_response_excerpt="ok",
                )
            )

            message = format_status(
                state,
                text_limit=700,
                latest_response=None,
                response_preview_chars=20,
                external_codex_activity=True,
            )

        self.assertIn("Local Codex activity: `active`", message)

    def test_format_status_without_history_still_reports_activity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = GatewayState(Path(temp_dir) / "gateway_state.json")

            message = format_status(
                state,
                text_limit=700,
                latest_response=None,
                response_preview_chars=20,
                external_codex_activity=True,
            )

        self.assertIn("State: `idle`", message)
        self.assertIn("Local Codex activity: `active`", message)

    def test_format_status_shows_blocking_reason_and_recovery_hint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = GatewayState(Path(temp_dir) / "gateway_state.json")
            state.finish_run(
                LastRunSummary(
                    run_id="run12345",
                    requester_user_id=1,
                    requester_name="tester",
                    prompt_excerpt="prompt",
                    started_at=datetime.now(timezone.utc),
                    finished_at=datetime.now(timezone.utc),
                    exit_code=None,
                    exit_signal="BLOCKED",
                    stdout_excerpt="",
                    stderr_excerpt="approval required",
                    assistant_response_excerpt="",
                )
            )

            message = format_status(
                state,
                text_limit=700,
                latest_response=None,
                response_preview_chars=20,
                blocking_reason="approval_required",
                recovery_hint="Approve the pending action in the terminal.",
            )

        self.assertIn("approval_required", message)
        self.assertIn("Approve the pending action", message)

    def test_format_status_shows_bound_model_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = GatewayState(Path(temp_dir) / "gateway_state.json")
            state.finish_run(
                LastRunSummary(
                    run_id="run12345",
                    requester_user_id=1,
                    requester_name="tester",
                    prompt_excerpt="prompt",
                    started_at=datetime.now(timezone.utc),
                    finished_at=datetime.now(timezone.utc),
                    exit_code=0,
                    exit_signal=None,
                    stdout_excerpt="",
                    stderr_excerpt="",
                    assistant_response_excerpt="ok",
                )
            )

            message = format_status(
                state,
                text_limit=700,
                latest_response=None,
                response_preview_chars=20,
                bound_model_profile="qwen3-8b",
            )

        self.assertIn("Bound model profile: `qwen3-8b`", message)


if __name__ == "__main__":
    unittest.main()
