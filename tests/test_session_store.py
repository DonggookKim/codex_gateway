from __future__ import annotations

import asyncio
import json
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
            store = SessionStore(root, root / "runtime")

            session = store.create_session(
                project_id="mail",
                label="triage",
                model_profile="gpt-5.4",
                execution_env="openai",
            )

            loaded = SessionStore(root, root / "runtime").load_session(
                "mail", session.session_id
            )

        self.assertIsInstance(loaded, SessionRecord)
        self.assertEqual(loaded.session_id, session.session_id)
        self.assertEqual(loaded.project_id, "mail")
        self.assertEqual(loaded.label, "triage")
        self.assertEqual(loaded.model_profile, "gpt-5.4")
        self.assertEqual(loaded.execution_env, "openai")
        self.assertEqual(loaded.status, "idle")
        self.assertTrue(str(loaded.codex_home_path).endswith(".codex"))
        self.assertIn(
            str(root / "runtime" / "projects" / "mail" / "sessions"),
            str(loaded.runtime_root),
        )
        self.assertIsNone(loaded.codex_thread_ref)
        self.assertIsNone(loaded.last_run_summary)
        self.assertFalse(loaded.archived)

    def test_create_session_uses_per_session_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            gateway_codex_home = root / "gateway-home" / ".codex"
            store = SessionStore(
                root / "state",
                root / "runtime",
                gateway_codex_home,
            )

            session = store.create_session(
                project_id="mail",
                label="triage",
                model_profile="gpt-5.4",
                execution_env="openai",
            )
            loaded = SessionStore(
                root / "state",
                root / "runtime",
                gateway_codex_home,
            ).load_session("mail", session.session_id)

        self.assertEqual(
            loaded.codex_home_path,
            root
            / "state"
            / "projects"
            / "mail"
            / "sessions"
            / session.session_id
            / "codex-home"
            / ".codex",
        )

    def test_default_runtime_root_uses_persistent_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = SessionStore(root)

        self.assertEqual(
            store.runtime_root,
            Path.home() / "codex_gateway_runtime",
        )

    def test_mark_blocked_persists_reason_and_recovery_hint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = SessionStore(root, root / "runtime")
            session = store.create_session(
                "mail",
                "triage",
                "gpt-5.4",
                execution_env="openai",
            )

            store.mark_blocked(
                "mail",
                session.session_id,
                waiting_on="external_input",
                blocking_reason="approval_required",
                recovery_hint="Approve the pending action in the local terminal.",
            )

            loaded = SessionStore(root, root / "runtime").load_session(
                "mail", session.session_id
            )

        self.assertEqual(loaded.waiting_on, "external_input")
        self.assertEqual(loaded.blocking_reason, "approval_required")
        self.assertIn("Approve", loaded.recovery_hint or "")
        self.assertEqual(loaded.status, "blocked")

    def test_import_session_preserves_explicit_session_id_and_thread_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = SessionStore(root, root / "runtime")

            store.import_session(
                project_id="mail",
                session_id="019legacythread",
                label="Legacy triage",
                model_profile="gpt-5.4",
                execution_env="openai",
                codex_thread_ref="019legacythread",
            )

            loaded = SessionStore(root, root / "runtime").load_session(
                "mail", "019legacythread"
            )

        self.assertEqual(loaded.session_id, "019legacythread")
        self.assertEqual(loaded.codex_thread_ref, "019legacythread")
        self.assertEqual(loaded.label, "Legacy triage")

    def test_update_codex_thread_ref_binds_new_thread_to_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = SessionStore(root, root / "runtime")
            session = store.create_session(
                "mail",
                "triage",
                "gpt-5.4",
                execution_env="openai",
            )

            store.update_codex_thread_ref(
                "mail",
                session.session_id,
                "019newthread",
            )

            loaded = SessionStore(root, root / "runtime").load_session(
                "mail", session.session_id
            )

        self.assertEqual(loaded.session_id, session.session_id)
        self.assertEqual(loaded.codex_thread_ref, "019newthread")

    def test_create_session_persists_local_execution_env(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = SessionStore(root, root / "runtime")

            session = store.create_session(
                project_id="mail",
                label="local-triage",
                model_profile="llama3.1:latest",
                execution_env="local_ollama",
            )

            loaded = SessionStore(root, root / "runtime").load_session(
                "mail", session.session_id
            )

        self.assertEqual(loaded.execution_env, "local_ollama")

    def test_materialize_session_codex_home_migrates_legacy_shared_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy_home = root / "legacy-home" / ".codex"
            legacy_sessions = legacy_home / "sessions" / "2026" / "04" / "24"
            legacy_sessions.mkdir(parents=True, exist_ok=True)
            (legacy_sessions / "rollout-019legacy.jsonl").write_text(
                '{"type":"session_meta","payload":{"id":"019legacy"}}\n',
                encoding="utf-8",
            )
            store = SessionStore(
                root / "state",
                root / "runtime",
                legacy_home,
            )
            session = store.import_session(
                project_id="mail",
                session_id="session-1",
                label="legacy",
                model_profile="gpt-5.4",
                execution_env="openai",
                codex_thread_ref="019legacy",
            )
            record_path = (
                root / "state" / "projects" / "mail" / "sessions" / "session-1" / "session.json"
            )
            payload = json.loads(record_path.read_text(encoding="utf-8"))
            payload["codex_home_path"] = str(legacy_home)
            record_path.write_text(json.dumps(payload), encoding="utf-8")

            loaded = SessionStore(
                root / "state",
                root / "runtime",
                legacy_home,
            ).materialize_session_codex_home(
                SessionStore(
                    root / "state",
                    root / "runtime",
                    legacy_home,
                ).load_session("mail", session.session_id)
            )

        self.assertNotEqual(loaded.codex_home_path, legacy_home)
        self.assertTrue(str(loaded.codex_home_path).endswith("codex-home/.codex"))

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
