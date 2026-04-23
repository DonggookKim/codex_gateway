from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from codex_gateway.session_store import SessionRecord, SessionStore
from codex_gateway.state import GatewayState


class SessionStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

    def tearDown(self) -> None:
        self._loop.close()
        asyncio.set_event_loop(None)

    def test_create_session_persists_and_loads_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = SessionStore(root)

            session = store.create_session(
                project_id="mail",
                label="triage",
                model_profile="gpt-5.4",
            )

            loaded = SessionStore(root).load_session("mail", session.session_id)

        self.assertIsInstance(loaded, SessionRecord)
        self.assertEqual(loaded.session_id, session.session_id)
        self.assertEqual(loaded.project_id, "mail")
        self.assertEqual(loaded.label, "triage")
        self.assertEqual(loaded.model_profile, "gpt-5.4")
        self.assertEqual(loaded.status, "idle")
        self.assertTrue(str(loaded.codex_home_path).endswith(".codex"))
        self.assertIn(
            "/tmp/codex_gateway_runtime/projects/mail/sessions/",
            str(loaded.runtime_root),
        )
        self.assertIsNone(loaded.codex_thread_ref)
        self.assertIsNone(loaded.last_run_summary)
        self.assertFalse(loaded.archived)

    def test_mark_blocked_persists_reason_and_recovery_hint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = SessionStore(root)
            session = store.create_session("mail", "triage", "gpt-5.4")

            store.mark_blocked(
                "mail",
                session.session_id,
                waiting_on="external_input",
                blocking_reason="approval_required",
                recovery_hint="Approve the pending action in the local terminal.",
            )

            loaded = SessionStore(root).load_session("mail", session.session_id)

        self.assertEqual(loaded.waiting_on, "external_input")
        self.assertEqual(loaded.blocking_reason, "approval_required")
        self.assertIn("Approve", loaded.recovery_hint or "")
        self.assertEqual(loaded.status, "blocked")

    def test_import_session_preserves_explicit_session_id_and_thread_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = SessionStore(root)

            store.import_session(
                project_id="mail",
                session_id="019legacythread",
                label="Legacy triage",
                model_profile="gpt-5.4",
                codex_thread_ref="019legacythread",
            )

            loaded = SessionStore(root).load_session("mail", "019legacythread")

        self.assertEqual(loaded.session_id, "019legacythread")
        self.assertEqual(loaded.codex_thread_ref, "019legacythread")
        self.assertEqual(loaded.label, "Legacy triage")

    def test_update_codex_thread_ref_binds_new_thread_to_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = SessionStore(root)
            session = store.create_session("mail", "triage", "gpt-5.4")

            store.update_codex_thread_ref(
                "mail",
                session.session_id,
                "019newthread",
            )

            loaded = SessionStore(root).load_session("mail", session.session_id)

        self.assertEqual(loaded.session_id, session.session_id)
        self.assertEqual(loaded.codex_thread_ref, "019newthread")

    def test_gateway_state_persists_selection_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "gateway_state.json"
            state = GatewayState(state_file)
            state.select_project("mail")
            state.select_session("sess-1")
            state.select_model_profile_for_new_session("gpt-5.4")

            reloaded = GatewayState(state_file)

        self.assertEqual(reloaded.selection_state["selected_project_id"], "mail")
        self.assertEqual(reloaded.selection_state["selected_session_id"], "sess-1")
        self.assertEqual(
            reloaded.selection_state["selected_model_profile_for_new_session"],
            "gpt-5.4",
        )


if __name__ == "__main__":
    unittest.main()
