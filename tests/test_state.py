from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_gateway.state import GatewayState


class GatewayWatchStateTest(unittest.TestCase):
    def test_enable_watch_persists_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = GatewayState(Path(temp_dir) / "gateway_state.json")

            state.enable_watch(
                "mail",
                "session-1",
                run_id="run-1",
                interval_seconds=600.0,
            )

            self.assertEqual(
                state.selection_state["watched_project_id"],
                "mail",
            )
            self.assertEqual(
                state.selection_state["watched_session_id"],
                "session-1",
            )
            self.assertEqual(
                state.selection_state["watched_run_id"],
                "run-1",
            )
            self.assertEqual(
                state.selection_state["watch_interval_seconds"],
                "600.0",
            )

    def test_select_project_clears_watch_when_project_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = GatewayState(Path(temp_dir) / "gateway_state.json")
            state.enable_watch(
                "mail",
                "session-1",
                run_id="run-1",
                interval_seconds=600.0,
            )

            state.select_project("other-project")

            self.assertIsNone(state.selection_state["watched_project_id"])
            self.assertIsNone(state.selection_state["watched_session_id"])
            self.assertIsNone(state.selection_state["watched_run_id"])
            self.assertIsNone(state.selection_state["watch_interval_seconds"])

    def test_select_session_clears_watch_when_session_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = GatewayState(Path(temp_dir) / "gateway_state.json")
            state.select_project("mail")
            state.enable_watch(
                "mail",
                "session-1",
                run_id="run-1",
                interval_seconds=600.0,
            )

            state.select_session("session-2")

            self.assertIsNone(state.selection_state["watched_project_id"])
            self.assertIsNone(state.selection_state["watched_session_id"])
            self.assertIsNone(state.selection_state["watched_run_id"])
            self.assertIsNone(state.selection_state["watch_interval_seconds"])


if __name__ == "__main__":
    unittest.main()
