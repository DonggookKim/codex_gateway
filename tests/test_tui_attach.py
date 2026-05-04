from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from codex_gateway.session_store import SessionStore
from codex_gateway.tui_attach import (
    AttachTarget,
    TuiAttachError,
    build_attach_command,
    build_resume_argv,
    infer_gateway_home_parent,
    resolve_attach_target,
    resolve_codex_thread_ref,
)


class TuiAttachTest(unittest.TestCase):
    def test_infer_gateway_home_parent_from_runtime_root(self) -> None:
        runtime_root = (
            Path.home() / "codex_gateway_runtime" / "projects" / "mail" / "sessions" / "abc123"
        )

        home_parent = infer_gateway_home_parent(runtime_root)

        self.assertEqual(
            home_parent,
            Path.home() / "codex_gateway_runtime" / "codex-home",
        )

    def test_resolve_codex_thread_ref_prefers_explicit_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = SessionStore(root / "state", root / "runtime")
            session = store.import_session(
                project_id="mail",
                session_id="abc123",
                label="triage",
                model_profile="gpt-5.4",
                codex_thread_ref="019thread-explicit",
            )

            resolved = resolve_codex_thread_ref(session)

        self.assertEqual(resolved, "019thread-explicit")

    def test_resolve_codex_thread_ref_falls_back_to_stdout_excerpt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = SessionStore(root / "state", root / "runtime")
            session = store.import_session(
                project_id="mail",
                session_id="abc123",
                label="triage",
                model_profile="gpt-5.4",
                codex_thread_ref=None,
            )
            store.update_last_run_summary(
                "mail",
                "abc123",
                {
                    "run_id": "run-1",
                    "requester_user_id": 1,
                    "requester_name": "tester",
                    "prompt_excerpt": "hello",
                    "started_at": "2026-04-24T00:00:00+00:00",
                    "finished_at": "2026-04-24T00:01:00+00:00",
                    "exit_code": 0,
                    "exit_signal": None,
                    "stdout_excerpt": (
                        '{"type":"thread.started",'
                        '"thread_id":"019thread-fallback"}'
                    ),
                    "stderr_excerpt": "",
                    "assistant_response_excerpt": "ok",
                    "codex_thread_ref": None,
                },
            )
            session = store.load_session("mail", "abc123")

            resolved = resolve_codex_thread_ref(session)

        self.assertEqual(resolved, "019thread-fallback")

    def test_build_attach_command_raises_when_thread_ref_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = SessionStore(root / "state", root / "runtime")
            session = store.import_session(
                project_id="mail",
                session_id="abc123",
                label="triage",
                model_profile="gpt-5.4",
                codex_thread_ref=None,
            )

            with self.assertRaises(TuiAttachError):
                build_attach_command(
                    repo_root=Path("/tmp/codex_sandbox"),
                    project_id="mail",
                    session=session,
                )

    def test_build_attach_command_uses_gateway_home_and_thread_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = SessionStore(root / "state", root / "runtime")
            session = store.import_session(
                project_id="mail",
                session_id="abc123",
                label="triage",
                model_profile="gpt-5.4",
                codex_thread_ref="019thread-explicit",
            )

            command = build_attach_command(
                repo_root=Path("/tmp/codex_sandbox"),
                project_id="mail",
                session=session,
            )

        self.assertEqual(
            command,
            "bash /tmp/codex_sandbox/codex_gateway/attach-gateway-session.sh mail abc123",
        )

    def test_build_resume_argv_uses_gpt_model_profile(self) -> None:
        argv = build_resume_argv(
            AttachTarget(
                project_id="mail",
                session_id="abc123",
                project_cwd=Path("/tmp/codex_sandbox"),
                home_parent=Path.home() / "codex_gateway_runtime" / "codex-home",
                thread_ref="019thread-explicit",
                model_profile="gpt-5.4",
                execution_env="openai",
            )
        )
        self.assertIn("-m", argv)
        self.assertIn("gpt-5.4", argv)

    def test_resolve_attach_target_rejects_unpersisted_thread_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_root = root / "state"
            runtime_root = root / "runtime"
            projects_file = state_root / "projects.json"
            store = SessionStore(state_root, runtime_root)
            store.import_session(
                project_id="mail",
                session_id="abc123",
                label="triage",
                model_profile="gpt-5.4",
                codex_thread_ref="019missing",
            )
            projects_file.parent.mkdir(parents=True, exist_ok=True)
            projects_file.write_text(
                (
                    '{"projects":[{"project_id":"mail","label":"Mail","cwd":"'
                    + str(root)
                    + '","project_channel_id":111,"default_model_profile":"gpt-5.4",'
                    '"allowed_model_profiles":["gpt-5.4"],"active_session_id":null,"archived":false}]}'
                ),
                encoding="utf-8",
            )

            with self.assertRaises(TuiAttachError):
                resolve_attach_target(
                    state_root=state_root,
                    projects_file=projects_file,
                    project_id="mail",
                    session_id="abc123",
                )

    def test_resolve_attach_target_prefers_recorded_codex_home_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_root = root / "state"
            runtime_root = root / "runtime"
            gateway_codex_home = root / "actual-home" / ".codex"
            projects_file = state_root / "projects.json"
            store = SessionStore(state_root, runtime_root, gateway_codex_home)
            store.import_session(
                project_id="mail",
                session_id="abc123",
                label="triage",
                model_profile="gpt-5.4",
                codex_thread_ref="019present",
            )
            record_path = (
                state_root / "projects" / "mail" / "sessions" / "abc123" / "session.json"
            )
            payload = json.loads(record_path.read_text(encoding="utf-8"))
            payload["codex_home_path"] = str(gateway_codex_home)
            record_path.write_text(json.dumps(payload), encoding="utf-8")
            sessions_dir = gateway_codex_home / "sessions" / "2026" / "04" / "24"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            (
                sessions_dir / "rollout-2026-04-24T00-00-00-019present.jsonl"
            ).write_text(
                '{"type":"session_meta","payload":{"id":"019present"}}\n',
                encoding="utf-8",
            )
            projects_file.parent.mkdir(parents=True, exist_ok=True)
            projects_file.write_text(
                (
                    '{"projects":[{"project_id":"mail","label":"Mail","cwd":"'
                    + str(root)
                    + '","project_channel_id":111,"default_model_profile":"gpt-5.4",'
                    '"allowed_model_profiles":["gpt-5.4"],"active_session_id":null,"archived":false}]}'
                ),
                encoding="utf-8",
            )

            target = resolve_attach_target(
                state_root=state_root,
                projects_file=projects_file,
                project_id="mail",
                session_id="abc123",
            )

        self.assertEqual(target.home_parent, gateway_codex_home.parent)
        self.assertEqual(target.thread_ref, "019present")

    def test_resolve_attach_target_falls_back_to_debug_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_root = root / "state"
            runtime_root = root / "runtime"
            projects_file = state_root / "projects.json"
            store = SessionStore(state_root, runtime_root)
            store.import_session(
                project_id="mail",
                session_id="abc123",
                label="triage",
                model_profile="gpt-5.4",
                codex_thread_ref="019debughome",
            )
            debug_home = root / "legacy-home"
            sessions_dir = debug_home / ".codex" / "sessions" / "2026" / "04" / "24"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            (
                sessions_dir / "rollout-2026-04-24T00-00-00-019debughome.jsonl"
            ).write_text(
                '{"type":"session_meta","payload":{"id":"019debughome"}}\n',
                encoding="utf-8",
            )
            (
                state_root
                / "projects"
                / "mail"
                / "sessions"
                / "abc123"
                / "artifacts"
                / "last_debug.json"
            ).write_text(
                '{"home":"' + str(debug_home) + '"}\n',
                encoding="utf-8",
            )
            projects_file.parent.mkdir(parents=True, exist_ok=True)
            projects_file.write_text(
                (
                    '{"projects":[{"project_id":"mail","label":"Mail","cwd":"'
                    + str(root)
                    + '","project_channel_id":111,"default_model_profile":"gpt-5.4",'
                    '"allowed_model_profiles":["gpt-5.4"],"active_session_id":null,"archived":false}]}'
                ),
                encoding="utf-8",
            )

            target = resolve_attach_target(
                state_root=state_root,
                projects_file=projects_file,
                project_id="mail",
                session_id="abc123",
            )

        self.assertEqual(target.home_parent, debug_home)
        self.assertEqual(target.thread_ref, "019debughome")


if __name__ == "__main__":
    unittest.main()
