from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from codex_gateway.bot import GatewayClient
from codex_gateway.config import GatewayConfig
from codex_gateway.project_registry import ProjectDefinition, ProjectRegistry
from codex_gateway.session_inspector import LatestCodexResponse
from codex_gateway.session_store import SessionStore
from codex_gateway.state import ActiveRun, GatewayState, LastRunSummary


class GatewayClientAskFlowTest(unittest.IsolatedAsyncioTestCase):
    def _make_client(self, root: Path) -> GatewayClient:
        config = GatewayConfig(
            discord_gateway_token="token",
            control_guild_id=1,
            control_channel_id=2,
            allowed_user_ids={3},
            codex_bin="codex",
            codex_cwd=root,
            codex_home_parent=None,
            codex_home_seed_from=None,
            codex_status_home=root / ".codex",
            prompt_max_chars=4000,
            status_text_max_chars=700,
            stream_tail_chars=2000,
            stop_sigint_grace_seconds=5.0,
            stop_sigterm_grace_seconds=5.0,
            response_preview_chars=20,
            state_root=root / "state",
            runtime_root=root / "runtime",
            projects_file=root / "state" / "projects.json",
            state_file=root / "gateway_state.json",
            tmp_dir=root / "tmp",
            last_response_file=root / "tmp" / "last_response.txt",
            prompt_preamble="test",
        )
        state = GatewayState(config.state_file)
        return GatewayClient(config=config, state=state)

    def _seed_project(
        self,
        root: Path,
        *,
        project_id: str = "mail",
        label: str = "Mail",
        default_model_profile: str = "gpt-5.4",
        allowed_model_profiles: list[str] | None = None,
        active_session_id: str | None = None,
    ) -> None:
        registry = ProjectRegistry(root / "state" / "projects.json")
        registry.save_projects(
            [
                ProjectDefinition(
                    project_id=project_id,
                    label=label,
                    cwd=root,
                    project_channel_id=111,
                    default_model_profile=default_model_profile,
                    allowed_model_profiles=allowed_model_profiles
                    or ["gpt-5.4", "qwen3-8b"],
                    active_session_id=active_session_id,
                    archived=False,
                )
            ]
        )

    def _seed_session(
        self,
        root: Path,
        *,
        project_id: str = "mail",
        label: str = "triage",
        model_profile: str = "gpt-5.4",
    ) -> str:
        store = SessionStore(root / "state", root / "runtime", root / ".codex")
        session = store.create_session(
            project_id=project_id,
            label=label,
            model_profile=model_profile,
            execution_env=(
                "local_ollama"
                if (
                    model_profile == "qwen3-8b"
                    or (":" in model_profile and not model_profile.startswith("gpt-"))
                )
                else "openai"
            ),
        )
        return session.session_id

    async def test_project_select_updates_global_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed_project(root)
            client = self._make_client(root)
            client.state.select_session("old-session")
            interaction = MagicMock()
            interaction.response.send_message = AsyncMock()

            await client._handle_project_select(interaction, "mail")

        self.assertEqual(client.state.selection_state["selected_project_id"], "mail")
        self.assertIsNone(client.state.selection_state["selected_session_id"])
        self.assertEqual(
            client.state.selection_state["selected_model_profile_for_new_session"],
            "gpt-5.4",
        )
        interaction.response.send_message.assert_awaited_once()
        sent_text = interaction.response.send_message.await_args.args[0]
        self.assertIn("Selected project", sent_text)
        self.assertIn("mail", sent_text)
        self.assertIn("Mail", sent_text)

    async def test_project_list_shows_registered_projects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed_project(root)
            client = self._make_client(root)
            interaction = MagicMock()
            interaction.response.send_message = AsyncMock()

            await client._handle_project_list(interaction)

        interaction.response.send_message.assert_awaited_once()
        sent_text = interaction.response.send_message.await_args.args[0]
        self.assertIn("Registered projects:", sent_text)
        self.assertIn("mail", sent_text)
        self.assertIn("Mail", sent_text)

    async def test_project_autocomplete_prefers_human_readable_label(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed_project(
                root,
                project_id="android_mail_arranger",
                label="Android Mail Arranger",
            )
            client = self._make_client(root)

            choices = await client._project_id_autocomplete(MagicMock(), "android")

        self.assertEqual(len(choices), 1)
        self.assertTrue(choices[0].name.startswith("Android Mail Arranger"))

    async def test_session_select_updates_selected_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed_project(root)
            session_id = self._seed_session(root)
            client = self._make_client(root)
            client.state.select_project("mail")
            interaction = MagicMock()
            interaction.response.send_message = AsyncMock()

            await client._handle_session_select(interaction, session_id)

        self.assertEqual(client.state.selection_state["selected_session_id"], session_id)
        interaction.response.send_message.assert_awaited_once()
        sent_text = interaction.response.send_message.await_args.args[0]
        self.assertIn(session_id, sent_text)

    async def test_session_list_shows_recent_prompt_excerpt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed_project(root)
            session_id = self._seed_session(root)
            client = self._make_client(root)
            client.state.select_project("mail")
            client.session_store.update_prompt_excerpt(
                "mail",
                session_id,
                "triage the latest alert thread",
            )
            interaction = MagicMock()
            interaction.response.send_message = AsyncMock()

            await client._handle_session_list(interaction)

        interaction.response.send_message.assert_awaited_once()
        sent_text = interaction.response.send_message.await_args.args[0]
        self.assertIn(session_id, sent_text)
        self.assertIn("triage the latest alert thread", sent_text)

    async def test_session_new_creates_and_selects_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed_project(root)
            client = self._make_client(root)
            client.state.select_project("mail")
            interaction = MagicMock()
            interaction.response.send_message = AsyncMock()

            await client._handle_session_new(interaction, "Nightly triage")

            selected_session_id = client.state.selection_state["selected_session_id"]
            self.assertIsNotNone(selected_session_id)
            loaded = client.session_store.load_session("mail", selected_session_id)
            self.assertEqual(loaded.label, "Nightly triage")
            self.assertEqual(loaded.model_profile, "gpt-5.4")
            self.assertEqual(loaded.execution_env, "openai")
            interaction.response.send_message.assert_awaited_once()
            sent_text = interaction.response.send_message.await_args.args[0]
            self.assertIn("Created and selected session", sent_text)
            self.assertIn("Nightly triage", sent_text)

    async def test_session_new_assigns_local_execution_env_for_ollama_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed_project(root, allowed_model_profiles=["gpt-5.4", "llama3.1:latest"])
            client = self._make_client(root)
            client.state.select_project("mail")
            client.state.select_model_profile_for_new_session("llama3.1:latest")
            interaction = MagicMock()
            interaction.response.send_message = AsyncMock()

            await client._handle_session_new(interaction, "Local triage")

            selected_session_id = client.state.selection_state["selected_session_id"]
            self.assertIsNotNone(selected_session_id)
            loaded = client.session_store.load_session("mail", selected_session_id)
            self.assertEqual(loaded.execution_env, "local_ollama")

    async def test_model_select_updates_selected_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed_project(root)
            client = self._make_client(root)
            client.state.select_project("mail")
            interaction = MagicMock()
            interaction.response.send_message = AsyncMock()

            await client._handle_model_select(interaction, "qwen3-8b")

        self.assertEqual(
            client.state.selection_state["selected_model_profile_for_new_session"],
            "qwen3-8b",
        )
        interaction.response.send_message.assert_awaited_once()

    async def test_tui_reports_attach_command_for_selected_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed_project(root)
            session_id = self._seed_session(root, label="alerts")
            client = self._make_client(root)
            client.state.select_project("mail")
            client.state.select_session(session_id)
            client.session_store.update_codex_thread_ref(
                "mail",
                session_id,
                "019thread-explicit",
            )
            session = client.session_store.load_session("mail", session_id)
            sessions_dir = (
                session.codex_home_path / "sessions" / "2026" / "04" / "24"
            )
            sessions_dir.mkdir(parents=True, exist_ok=True)
            (sessions_dir / "rollout-2026-04-24T00-00-00-019thread-explicit.jsonl").write_text(
                '{"type":"session_meta","payload":{"id":"019thread-explicit"}}\n',
                encoding="utf-8",
            )
            interaction = MagicMock()
            interaction.response.send_message = AsyncMock()

            await client._handle_tui(interaction)

        interaction.response.send_message.assert_awaited_once()
        sent_text = interaction.response.send_message.await_args.args[0]
        self.assertIn("attach-gateway-session.sh", sent_text)
        self.assertIn("mail", sent_text)
        self.assertIn(session_id, sent_text)
        self.assertIn("019thread-explicit", sent_text)

    async def test_run_ask_recovers_from_missing_rollout_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed_project(root)
            session_id = self._seed_session(root, label="alerts")
            client = self._make_client(root)
            client.session_store.update_codex_thread_ref(
                "mail",
                session_id,
                "019stale-thread",
            )

            stale_summary = LastRunSummary(
                run_id="run-stale",
                requester_user_id=3,
                requester_name="tester",
                prompt_excerpt="hello",
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
                exit_code=1,
                exit_signal=None,
                stdout_excerpt="",
                stderr_excerpt="Error: thread/resume failed: no rollout found for thread id 019stale-thread",
                assistant_response_excerpt="",
                codex_thread_ref="019stale-thread",
            )
            fresh_summary = LastRunSummary(
                run_id="run-fresh",
                requester_user_id=3,
                requester_name="tester",
                prompt_excerpt="hello",
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
                exit_code=0,
                exit_signal=None,
                stdout_excerpt="",
                stderr_excerpt="",
                assistant_response_excerpt="ok",
                codex_thread_ref="019fresh-thread",
            )

            with patch(
                "codex_gateway.bot.run_codex",
                new=AsyncMock(side_effect=[stale_summary, fresh_summary]),
            ) as run_codex_mock:
                await client._run_ask(
                    requester_id=3,
                    requester_name="tester",
                    prompt="hello",
                    project_id="mail",
                    session_id=session_id,
                    codex_session_ref="019stale-thread",
                    model_profile="gpt-5.4",
                    project_label="Mail",
                    session_label="alerts",
                )

            self.assertEqual(run_codex_mock.await_count, 2)
            reloaded = client.session_store.load_session("mail", session_id)
            self.assertEqual(reloaded.codex_thread_ref, "019fresh-thread")
            self.assertEqual(
                reloaded.last_run_summary["run_id"],
                "run-fresh",
            )

    async def test_current_reports_selected_project_session_and_watch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed_project(root, label="Mail")
            session_id = self._seed_session(root, label="alerts", model_profile="qwen3-8b")
            client = self._make_client(root)
            client.state.select_project("mail")
            client.state.select_session(session_id)
            client.state.enable_watch(
                "mail",
                session_id,
                run_id="run-1",
                interval_seconds=30.0,
            )
            client.session_store.update_codex_thread_ref(
                "mail",
                session_id,
                "019thread-explicit",
            )
            interaction = MagicMock()
            interaction.response.send_message = AsyncMock()

            await client._handle_current(interaction)

        interaction.response.send_message.assert_awaited_once()
        sent_text = interaction.response.send_message.await_args.args[0]
        self.assertIn("Current selection", sent_text)
        self.assertIn("Project: `mail`", sent_text)
        self.assertIn("Session: `", sent_text)
        self.assertIn("alerts", sent_text)
        self.assertIn("Model: `qwen3-8b`", sent_text)
        self.assertIn("Execution env: `local_ollama`", sent_text)
        self.assertIn("Watch: `on`", sent_text)
        self.assertIn("30s", sent_text)
        self.assertIn("019thread-explicit", sent_text)

    async def test_watch_on_tracks_selected_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed_project(root)
            session_id = self._seed_session(root)
            client = self._make_client(root)
            client.state.select_project("mail")
            client.state.select_session(session_id)
            interaction = MagicMock()
            interaction.response.send_message = AsyncMock()

            await client._handle_watch(interaction, "on", None)

        self.assertEqual(
            client.state.selection_state["watched_project_id"],
            "mail",
        )
        self.assertEqual(
            client.state.selection_state["watched_session_id"],
            session_id,
        )
        self.assertEqual(
            client.state.selection_state["watch_interval_seconds"],
            "600.0",
        )
        interaction.response.send_message.assert_awaited_once()
        sent_text = interaction.response.send_message.await_args.args[0]
        self.assertIn("Watch enabled", sent_text)

    async def test_watch_on_accepts_custom_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed_project(root)
            session_id = self._seed_session(root)
            client = self._make_client(root)
            client.state.select_project("mail")
            client.state.select_session(session_id)
            interaction = MagicMock()
            interaction.response.send_message = AsyncMock()

            await client._handle_watch(interaction, "on", "30s")

        self.assertEqual(
            client.state.selection_state["watch_interval_seconds"],
            "30.0",
        )

    async def test_model_select_accepts_discovered_ollama_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed_project(root, allowed_model_profiles=["gpt-5.4"])
            client = self._make_client(root)
            client.config = GatewayConfig(
                **{
                    **client.config.__dict__,
                    "discovered_ollama_models": ("qwen3:8b", "llama3.1:latest"),
                }
            )
            client.state.select_project("mail")
            interaction = MagicMock()
            interaction.response.send_message = AsyncMock()

            await client._handle_model_select(interaction, "llama3.1:latest")

        self.assertEqual(
            client.state.selection_state["selected_model_profile_for_new_session"],
            "llama3.1:latest",
        )

    async def test_project_select_turns_watch_off_when_project_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            registry = ProjectRegistry(root / "state" / "projects.json")
            registry.save_projects(
                [
                    ProjectDefinition(
                        project_id="mail",
                        label="Mail",
                        cwd=root,
                        project_channel_id=111,
                        default_model_profile="gpt-5.4",
                        allowed_model_profiles=["gpt-5.4", "qwen3-8b"],
                        active_session_id=None,
                        archived=False,
                    ),
                    ProjectDefinition(
                        project_id="other",
                        label="Other",
                        cwd=root,
                        project_channel_id=222,
                        default_model_profile="gpt-5.4",
                        allowed_model_profiles=["gpt-5.4", "qwen3-8b"],
                        active_session_id=None,
                        archived=False,
                    ),
                ]
            )
            session_id = self._seed_session(root)
            client = self._make_client(root)
            client.state.select_project("mail")
            client.state.select_session(session_id)
            client.state.enable_watch(
                "mail",
                session_id,
                run_id="run-1",
                interval_seconds=600.0,
            )
            interaction = MagicMock()
            interaction.response.send_message = AsyncMock()

            await client._handle_project_select(interaction, "other")

        self.assertIsNone(client.state.selection_state["watched_project_id"])
        self.assertIsNone(client.state.selection_state["watched_session_id"])

    async def test_check_watch_once_notifies_on_successful_idle_transition(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed_project(root)
            session_id = self._seed_session(root)
            client = self._make_client(root)
            store = SessionStore(root / "state", root / "runtime")
            session = store.load_session("mail", session_id)
            session.last_response_path.write_text("done", encoding="utf-8-sig")
            session.last_response_path.with_name("last_stderr.txt").write_text(
                "warn",
                encoding="utf-8-sig",
            )
            store.update_last_run_summary(
                "mail",
                session_id,
                {
                    "run_id": "run-1",
                    "requester_user_id": 3,
                    "requester_name": "tester",
                    "prompt_excerpt": "hello",
                    "started_at": "2026-04-23T00:00:00+00:00",
                    "finished_at": "2026-04-23T00:01:00+00:00",
                    "exit_code": 0,
                    "exit_signal": None,
                    "stdout_excerpt": "",
                    "stderr_excerpt": "",
                    "assistant_response_excerpt": "done",
                    "codex_thread_ref": None,
                },
            )
            client.state.enable_watch(
                "mail",
                session_id,
                run_id="run-1",
                interval_seconds=600.0,
            )
            channel = MagicMock()
            channel.send = AsyncMock()
            client.get_channel = MagicMock(return_value=channel)

            await client._check_watch_once()

        channel.send.assert_awaited_once()
        kwargs = channel.send.await_args.kwargs
        self.assertIn("Watch complete", kwargs["content"])
        self.assertIn("mail", kwargs["content"])
        self.assertEqual(len(kwargs["files"]), 1)
        self.assertEqual(kwargs["files"][0].filename, "last_response.txt")
        self.assertEqual(
            client.state.selection_state["watched_project_id"],
            "mail",
        )

    async def test_check_watch_once_notifies_on_failed_idle_transition(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed_project(root)
            session_id = self._seed_session(root)
            client = self._make_client(root)
            store = SessionStore(root / "state", root / "runtime")
            session = store.load_session("mail", session_id)
            stderr_path = session.last_response_path.with_name("last_stderr.txt")
            stderr_path.write_text("traceback", encoding="utf-8-sig")
            store.update_last_run_summary(
                "mail",
                session_id,
                {
                    "run_id": "run-2",
                    "requester_user_id": 3,
                    "requester_name": "tester",
                    "prompt_excerpt": "hello",
                    "started_at": "2026-04-23T00:00:00+00:00",
                    "finished_at": "2026-04-23T00:01:00+00:00",
                    "exit_code": 1,
                    "exit_signal": None,
                    "stdout_excerpt": "",
                    "stderr_excerpt": "unsupported call",
                    "assistant_response_excerpt": "",
                    "codex_thread_ref": None,
                },
            )
            client.state.enable_watch(
                "mail",
                session_id,
                run_id="run-2",
                interval_seconds=600.0,
            )
            channel = MagicMock()
            channel.send = AsyncMock()
            client.get_channel = MagicMock(return_value=channel)

            await client._check_watch_once()

        channel.send.assert_awaited_once()
        kwargs = channel.send.await_args.kwargs
        self.assertIn("Exit code: `1`", kwargs["content"])
        self.assertIn("unsupported call", kwargs["content"])
        self.assertEqual(len(kwargs["files"]), 1)

    async def test_check_watch_once_sends_snapshot_when_still_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed_project(root)
            session_id = self._seed_session(root, model_profile="qwen3-8b")
            client = self._make_client(root)
            client.state.enable_watch(
                "mail",
                session_id,
                run_id="run-3",
                interval_seconds=30.0,
            )
            client.state.set_active_run(
                ActiveRun(
                    run_id="run-3",
                    requester_user_id=3,
                    requester_name="tester",
                    prompt_excerpt="hello",
                    started_at=datetime.now(timezone.utc),
                    pid=1234,
                    last_message_path=root / "tmp" / "last.txt",
                    project_id="mail",
                    session_id=session_id,
                    model_profile="qwen3-8b",
                    stderr_tail="",
                ),
                project_id="mail",
            )
            channel = MagicMock()
            channel.send = AsyncMock()
            client.get_channel = MagicMock(return_value=channel)

            await client._check_watch_once()

        channel.send.assert_awaited_once()
        kwargs = channel.send.await_args.kwargs
        self.assertIn("Watch snapshot", kwargs["content"])
        self.assertIn("Bound model profile: `qwen3-8b`", kwargs["content"])

    async def test_persist_session_artifacts_copies_debug_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed_project(root)
            session_id = self._seed_session(root)
            client = self._make_client(root)
            store = SessionStore(root / "state", root / "runtime")
            session = store.load_session("mail", session_id)
            debug_path = session.runtime_root / "tmp" / "run-1-debug.json"
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            debug_path.write_text('{"argv":["codex"]}', encoding="utf-8")

            summary = LastRunSummary(
                run_id="run-1",
                requester_user_id=3,
                requester_name="tester",
                prompt_excerpt="hello",
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
                exit_code=0,
                exit_signal=None,
                stdout_excerpt="",
                stderr_excerpt="",
                assistant_response_excerpt="ok",
            )

            client._persist_session_artifacts(session, "run-1", summary)

            copied = session.last_response_path.with_name("last_debug.json")
            self.assertTrue(copied.exists())

    async def test_run_watch_timer_repeats_until_idle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client = self._make_client(root)

            with patch(
                "codex_gateway.bot.asyncio.sleep",
                new=AsyncMock(),
            ) as sleep_mock, patch.object(
                client,
                "_check_watch_once",
                new=AsyncMock(side_effect=[True, False]),
            ) as check_mock:
                await client._run_watch_timer(30.0)

        self.assertEqual(sleep_mock.await_count, 2)
        self.assertEqual(check_mock.await_count, 2)

    async def test_session_autocomplete_includes_recent_prompt_excerpt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed_project(root)
            session_id = self._seed_session(root, label="alerts")
            client = self._make_client(root)
            client.state.select_project("mail")
            client.session_store.update_prompt_excerpt(
                "mail",
                session_id,
                "please inspect new discord gateway regressions",
            )

            choices = await client._session_id_autocomplete(MagicMock(), "discord")

        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0].value, session_id)
        self.assertIn("discord gateway regressions", choices[0].name)

    async def test_resolve_project_channel_name_uses_project_channel_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed_project(root, project_id="android_mail_arranger", label="Android Mail Arranger")
            client = self._make_client(root)
            project = client._load_project_definition("android_mail_arranger")
            channel = MagicMock()
            channel.name = "android-mail-arranger"
            client.get_channel = MagicMock(return_value=channel)

            resolved = await client._resolve_project_channel_name(project)

        self.assertEqual(resolved, "android-mail-arranger")
        client.get_channel.assert_called_once_with(111)

    async def test_run_ask_does_not_post_completion_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client = self._make_client(root)
            client.get_channel = MagicMock()
            client.fetch_channel = AsyncMock()

            with patch("codex_gateway.bot.run_codex", new=AsyncMock()):
                await client._run_ask(
                    requester_id=3,
                    requester_name="tester",
                    prompt="hello",
                )

        client.get_channel.assert_not_called()
        client.fetch_channel.assert_not_called()

    async def test_handle_status_defers_before_followup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client = self._make_client(root)
            interaction = MagicMock()
            interaction.response.defer = AsyncMock()
            interaction.followup.send = AsyncMock()

            latest = LatestCodexResponse(
                session_id="session-1",
                timestamp="2026-04-21T00:00:00Z",
                text="01234567890123456789ABCDEFGHIJ",
            )

            with patch(
                "codex_gateway.bot.inspect_latest_codex_response",
                return_value=latest,
            ), patch.object(
                client,
                "_has_external_codex_activity",
                return_value=False,
            ):
                await client._handle_status(interaction)

        interaction.response.defer.assert_awaited_once_with()
        interaction.followup.send.assert_awaited_once()

    async def test_handle_ask_rejects_when_external_codex_activity_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed_project(root)
            session_id = self._seed_session(root)
            client = self._make_client(root)
            interaction = MagicMock()
            interaction.user.id = 3
            interaction.user.display_name = "tester"
            interaction.channel = object()
            interaction.response.defer = AsyncMock()
            interaction.followup.send = AsyncMock()
            client.state.select_project("mail")
            client.state.select_session(session_id)

            with patch.object(
                client,
                "_has_external_codex_activity",
                return_value=True,
            ):
                await client._handle_ask(interaction, "hello")

        interaction.response.defer.assert_awaited_once_with()
        interaction.followup.send.assert_awaited_once()
        sent_text = interaction.followup.send.await_args.kwargs["content"]
        self.assertIn("Another Codex process appears active", sent_text)

    async def test_handle_ask_acknowledges_selected_target_and_prompt_excerpt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed_project(root)
            session_id = self._seed_session(root, label="alerts")
            client = self._make_client(root)
            interaction = MagicMock()
            interaction.user.id = 3
            interaction.user.display_name = "tester"
            interaction.channel = object()
            interaction.response.defer = AsyncMock()
            interaction.followup.send = AsyncMock()
            client.state.select_project("mail")
            client.state.select_session(session_id)

            with patch.object(
                client,
                "_has_external_codex_activity",
                return_value=False,
            ), patch(
                "codex_gateway.bot.asyncio.create_task",
                side_effect=lambda coro: (coro.close(), MagicMock())[1],
            ) as create_task_mock:
                await client._handle_ask(interaction, "please triage the new alerts")

        interaction.followup.send.assert_awaited_once()
        sent_text = interaction.followup.send.await_args.kwargs["content"]
        self.assertIn(f"`mail/{session_id}`", sent_text)
        self.assertIn("please triage the new alerts", sent_text)
        create_task_mock.assert_called_once()

    async def test_run_ask_binds_new_codex_thread_ref_to_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed_project(root)
            session_id = self._seed_session(root, label="alerts")
            client = self._make_client(root)

            with patch(
                "codex_gateway.bot.run_codex",
                new=AsyncMock(
                    return_value=LastRunSummary(
                        run_id="run-1",
                        requester_user_id=3,
                        requester_name="tester",
                        prompt_excerpt="hello",
                        started_at=None,
                        finished_at=None,
                        exit_code=0,
                        exit_signal=None,
                        stdout_excerpt="",
                        stderr_excerpt="",
                        assistant_response_excerpt="done",
                        codex_thread_ref="019newthread",
                    )
                ),
            ):
                await client._run_ask(
                    requester_id=3,
                    requester_name="tester",
                    prompt="hello",
                    project_id="mail",
                    session_id=session_id,
                    codex_session_ref=None,
                    project_label="Mail",
                    session_label="alerts",
                )
            loaded = client.session_store.load_session("mail", session_id)
            self.assertEqual(loaded.codex_thread_ref, "019newthread")

    async def test_run_ask_passes_project_discord_channel_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed_project(root)
            session_id = self._seed_session(root, label="alerts")
            client = self._make_client(root)

            with patch(
                "codex_gateway.bot.run_codex",
                new=AsyncMock(
                    return_value=LastRunSummary(
                        run_id="run-1",
                        requester_user_id=3,
                        requester_name="tester",
                        prompt_excerpt="hello",
                        started_at=None,
                        finished_at=None,
                        exit_code=0,
                        exit_signal=None,
                        stdout_excerpt="",
                        stderr_excerpt="",
                        assistant_response_excerpt="done",
                        codex_thread_ref="019existingthread",
                    )
                ),
            ) as run_codex_mock:
                await client._run_ask(
                    requester_id=3,
                    requester_name="tester",
                    prompt="hello",
                    project_id="mail",
                    session_id=session_id,
                    codex_session_ref="019existingthread",
                    discord_channel_name="mail-project",
                    project_label="Mail",
                    session_label="alerts",
                )

        self.assertEqual(
            run_codex_mock.await_args.kwargs["discord_channel_name"],
            "mail-project",
        )

    async def test_run_ask_passes_session_model_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._seed_project(root)
            session_id = self._seed_session(root, label="alerts", model_profile="qwen3-8b")
            client = self._make_client(root)

            with patch(
                "codex_gateway.bot.run_codex",
                new=AsyncMock(
                    return_value=LastRunSummary(
                        run_id="run-1",
                        requester_user_id=3,
                        requester_name="tester",
                        prompt_excerpt="hello",
                        started_at=None,
                        finished_at=None,
                        exit_code=0,
                        exit_signal=None,
                        stdout_excerpt="",
                        stderr_excerpt="",
                        assistant_response_excerpt="done",
                        codex_thread_ref="019existingthread",
                    )
                ),
            ) as run_codex_mock:
                await client._run_ask(
                    requester_id=3,
                    requester_name="tester",
                    prompt="hello",
                    project_id="mail",
                    session_id=session_id,
                    codex_session_ref="019existingthread",
                    model_profile="qwen3-8b",
                    project_label="Mail",
                    session_label="alerts",
                )

        self.assertEqual(
            run_codex_mock.await_args.kwargs["model_profile"],
            "qwen3-8b",
        )

    async def test_safe_defer_swallows_unknown_interaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client = self._make_client(root)
            interaction = MagicMock()
            interaction.response.defer = AsyncMock(
                side_effect=discord.NotFound(
                    response=MagicMock(),
                    message="Unknown interaction",
                )
            )

            ok = await client._safe_defer(interaction)

        self.assertFalse(ok)

    async def test_safe_followup_send_omits_file_when_none(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client = self._make_client(root)
            interaction = MagicMock()
            interaction.followup.send = AsyncMock()

            ok = await client._safe_followup_send(interaction, "hello")

        self.assertTrue(ok)
        interaction.followup.send.assert_awaited_once()
        self.assertEqual(
            interaction.followup.send.await_args.kwargs,
            {"content": "hello"},
        )


if __name__ == "__main__":
    unittest.main()
