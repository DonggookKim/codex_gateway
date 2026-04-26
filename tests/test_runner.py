from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from codex_gateway.config import GatewayConfig
from codex_gateway.runner import (
    _prepare_runtime_home,
    build_command,
    prepare_runtime_home_dir,
    run_codex,
)
from codex_gateway.state import GatewayState


class RunnerCommandTest(unittest.TestCase):
    def test_build_command_targets_explicit_session(self) -> None:
        command = build_command(
            codex_bin="codex",
            session_ref="session-123",
            prompt="hello",
            last_message_path=Path("/tmp/last.txt"),
        )

        self.assertEqual(command[:4], ["codex", "exec", "resume", "session-123"])
        self.assertNotIn("--last", command)

    def test_build_command_starts_new_json_session_without_resume(self) -> None:
        command = build_command(
            codex_bin="codex",
            session_ref=None,
            prompt="hello",
            last_message_path=Path("/tmp/last.txt"),
        )

        self.assertEqual(command[:3], ["codex", "exec", "--json"])
        self.assertNotIn("resume", command)

    def test_build_command_includes_project_discord_channel_override(self) -> None:
        command = build_command(
            codex_bin="codex",
            session_ref="session-123",
            prompt="hello",
            last_message_path=Path("/tmp/last.txt"),
            discord_channel_name="android-mail-arranger",
        )

        self.assertIn("-c", command)
        self.assertIn(
            'mcp_servers.discord.env.DISCORD_CHANNEL="android-mail-arranger"',
            command,
        )

    def test_build_command_uses_local_profile_for_qwen3_8b(self) -> None:
        command = build_command(
            codex_bin="codex",
            session_ref="session-123",
            prompt="hello",
            last_message_path=Path("/tmp/last.txt"),
            model_profile="qwen3-8b",
        )

        self.assertIn("-p", command)
        self.assertIn("ollama-qwen25-coder", command)
        self.assertIn("-m", command)
        self.assertIn("qwen3:8b", command)
        self.assertLess(command.index("-p"), command.index("resume"))

    def test_build_command_uses_local_profile_for_discovered_ollama_model(self) -> None:
        command = build_command(
            codex_bin="codex",
            session_ref="session-123",
            prompt="hello",
            last_message_path=Path("/tmp/last.txt"),
            model_profile="llama3.1:latest",
            local_model_profiles={"qwen3:8b", "llama3.1:latest"},
        )

        self.assertIn("-p", command)
        self.assertIn("ollama-qwen25-coder", command)
        self.assertIn("-m", command)
        self.assertIn("llama3.1:latest", command)
        self.assertLess(command.index("-p"), command.index("resume"))

    def test_prepare_runtime_home_preserves_existing_codex_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home_parent = root / "runtime-home"
            codex_dir = home_parent / ".codex"
            marker = codex_dir / "sessions" / "keep.txt"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("keep", encoding="utf-8")

            seed_root = root / "seed-home"
            seed_codex_dir = seed_root / ".codex"
            seed_codex_dir.mkdir(parents=True, exist_ok=True)
            (seed_codex_dir / "config.toml").write_text("model = 'qwen'\n", encoding="utf-8")

            config = GatewayConfig(
                discord_gateway_token="token",
                control_guild_id=1,
                control_channel_id=2,
                allowed_user_ids={3},
                codex_bin="codex",
                codex_cwd=root,
                codex_home_parent=home_parent,
                codex_home_seed_from=seed_codex_dir,
                codex_status_home=root,
                prompt_max_chars=4000,
                status_text_max_chars=700,
                stream_tail_chars=2000,
                stop_sigint_grace_seconds=5.0,
                stop_sigterm_grace_seconds=5.0,
                response_preview_chars=20,
                state_file=root / "state.json",
                tmp_dir=root / "tmp",
                last_response_file=root / "tmp" / "last_response.txt",
                prompt_preamble="test",
            )

            env = _prepare_runtime_home(config)
            self.assertEqual(env, {"HOME": str(home_parent)})
            self.assertTrue(marker.exists())

    def test_prepare_runtime_home_links_shared_auth_instead_of_copying_seed_auth(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home_parent = root / "runtime-home"
            seed_root = root / "seed-home"
            seed_codex_dir = seed_root / ".codex"
            seed_codex_dir.mkdir(parents=True, exist_ok=True)
            (seed_codex_dir / "config.toml").write_text("model = 'gpt-5.4'\n", encoding="utf-8")
            (seed_codex_dir / "auth.json").write_text('{"stale":true}\n', encoding="utf-8")

            shared_auth = root / "global-home" / ".codex" / "auth.json"
            shared_auth.parent.mkdir(parents=True, exist_ok=True)
            shared_auth.write_text('{"fresh":true}\n', encoding="utf-8")

            config = GatewayConfig(
                discord_gateway_token="token",
                control_guild_id=1,
                control_channel_id=2,
                allowed_user_ids={3},
                codex_bin="codex",
                codex_cwd=root,
                codex_home_parent=home_parent,
                codex_home_seed_from=seed_codex_dir,
                codex_status_home=root,
                prompt_max_chars=4000,
                status_text_max_chars=700,
                stream_tail_chars=2000,
                stop_sigint_grace_seconds=5.0,
                stop_sigterm_grace_seconds=5.0,
                response_preview_chars=20,
                state_file=root / "state.json",
                tmp_dir=root / "tmp",
                last_response_file=root / "tmp" / "last_response.txt",
                prompt_preamble="test",
            )

            with patch.dict(os.environ, {"CODEX_SHARED_AUTH_SOURCE": str(shared_auth)}):
                env = _prepare_runtime_home(config)

            target_auth = home_parent / ".codex" / "auth.json"
            self.assertEqual(env, {"HOME": str(home_parent)})
            self.assertTrue(target_auth.is_symlink())
            self.assertEqual(target_auth.resolve(), shared_auth.resolve())
            self.assertEqual(target_auth.read_text(encoding="utf-8"), '{"fresh":true}\n')

    def test_prepare_runtime_home_removes_auth_for_local_execution_env(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home_parent = root / "runtime-home"
            seed_root = root / "seed-home"
            seed_codex_dir = seed_root / ".codex"
            seed_codex_dir.mkdir(parents=True, exist_ok=True)
            (seed_codex_dir / "config.toml").write_text(
                "model = 'qwen3:8b'\n",
                encoding="utf-8",
            )
            (seed_codex_dir / "auth.json").write_text(
                '{"stale":true}\n',
                encoding="utf-8",
            )

            prepare_runtime_home_dir(
                home_parent=home_parent,
                seed_from=seed_codex_dir,
                shared_auth_source=None,
                use_shared_auth=False,
            )

            target_auth = home_parent / ".codex" / "auth.json"
            self.assertFalse(target_auth.exists())


class RunnerSessionRefTest(unittest.IsolatedAsyncioTestCase):
    async def test_run_codex_blocks_without_selected_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = GatewayConfig(
                discord_gateway_token="token",
                control_guild_id=1,
                control_channel_id=2,
                allowed_user_ids={3},
                codex_bin="codex",
                codex_cwd=root,
                codex_home_parent=None,
                codex_home_seed_from=None,
                codex_status_home=root,
                prompt_max_chars=4000,
                status_text_max_chars=700,
                stream_tail_chars=2000,
                stop_sigint_grace_seconds=5.0,
                stop_sigterm_grace_seconds=5.0,
                response_preview_chars=20,
                state_file=root / "state.json",
                tmp_dir=root / "tmp",
                last_response_file=root / "tmp" / "last_response.txt",
                prompt_preamble="test",
            )
            state = GatewayState(config.state_file)
            notifications: list[str] = []

            summary = await run_codex(
                state=state,
                config=config,
                requester_user_id=3,
                requester_name="tester",
                prompt="hello",
                project_label="Mail",
                session_label="triage",
                project_notification_sink=notifications.append,
            )

        self.assertEqual(summary.exit_signal, "BLOCKED")
        self.assertEqual(state.last_run.exit_signal, "BLOCKED")
        self.assertEqual(len(notifications), 1)
        self.assertIn("Project: `Mail`", notifications[0])
        self.assertIn("Session: `triage`", notifications[0])
        self.assertIn("Blocked:", notifications[0])

    async def test_run_codex_uses_selected_session_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = GatewayConfig(
                discord_gateway_token="token",
                control_guild_id=1,
                control_channel_id=2,
                allowed_user_ids={3},
                codex_bin="codex",
                codex_cwd=root,
                codex_home_parent=None,
                codex_home_seed_from=None,
                codex_status_home=root,
                prompt_max_chars=4000,
                status_text_max_chars=700,
                stream_tail_chars=2000,
                stop_sigint_grace_seconds=5.0,
                stop_sigterm_grace_seconds=5.0,
                response_preview_chars=20,
                state_file=root / "state.json",
                tmp_dir=root / "tmp",
                last_response_file=root / "tmp" / "last_response.txt",
                prompt_preamble="test",
            )
            state = GatewayState(config.state_file)
            state.select_session("session-123")

            fake_process = AsyncMock()
            fake_process.pid = 4321
            fake_process.stdout = None
            fake_process.stderr = None
            fake_process.wait = AsyncMock(return_value=0)

            with patch(
                "codex_gateway.runner.build_command",
                return_value=[
                    "codex",
                    "exec",
                    "resume",
                    "session-123",
                    "--skip-git-repo-check",
                    "-o",
                    str(root / "tmp" / "run-last.txt"),
                    "test\n\nUser request:\nhello",
                ],
            ) as build_command_mock, patch(
                "codex_gateway.runner.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=fake_process),
            ) as create_subprocess_exec_mock:
                await run_codex(
                    state=state,
                    config=config,
                    requester_user_id=3,
                    requester_name="tester",
                    prompt="hello",
                    project_label="Mail",
                    session_label="triage",
                )

        build_command_mock.assert_called_once()
        self.assertEqual(
            create_subprocess_exec_mock.await_args.kwargs["stdin"],
            asyncio.subprocess.DEVNULL,
        )
        self.assertEqual(
            build_command_mock.call_args.args[1],
            "session-123",
        )

    async def test_run_codex_captures_thread_ref_for_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = GatewayConfig(
                discord_gateway_token="token",
                control_guild_id=1,
                control_channel_id=2,
                allowed_user_ids={3},
                codex_bin="codex",
                codex_cwd=root,
                codex_home_parent=None,
                codex_home_seed_from=None,
                codex_status_home=root,
                prompt_max_chars=4000,
                status_text_max_chars=700,
                stream_tail_chars=2000,
                stop_sigint_grace_seconds=5.0,
                stop_sigterm_grace_seconds=5.0,
                response_preview_chars=20,
                state_file=root / "state.json",
                tmp_dir=root / "tmp",
                last_response_file=root / "tmp" / "last_response.txt",
                prompt_preamble="test",
            )
            state = GatewayState(config.state_file)
            stdout = asyncio.StreamReader()
            stdout.feed_data(
                b'{"type":"session_meta","payload":{"id":"019newthread"}}\n'
            )
            stdout.feed_eof()
            stderr = asyncio.StreamReader()
            stderr.feed_eof()

            fake_process = MagicMock()
            fake_process.pid = 4321
            fake_process.stdout = stdout
            fake_process.stderr = stderr
            fake_process.wait = AsyncMock(return_value=0)

            with patch(
                "codex_gateway.runner.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=fake_process),
            ):
                summary = await run_codex(
                    state=state,
                    config=config,
                    requester_user_id=3,
                    requester_name="tester",
                    prompt="hello",
                    start_new_session=True,
                )

        self.assertEqual(summary.codex_thread_ref, "019newthread")

    async def test_run_codex_captures_thread_started_ref_for_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = GatewayConfig(
                discord_gateway_token="token",
                control_guild_id=1,
                control_channel_id=2,
                allowed_user_ids={3},
                codex_bin="codex",
                codex_cwd=root,
                codex_home_parent=None,
                codex_home_seed_from=None,
                codex_status_home=root,
                prompt_max_chars=4000,
                status_text_max_chars=700,
                stream_tail_chars=2000,
                stop_sigint_grace_seconds=5.0,
                stop_sigterm_grace_seconds=5.0,
                response_preview_chars=20,
                state_file=root / "state.json",
                tmp_dir=root / "tmp",
                last_response_file=root / "tmp" / "last_response.txt",
                prompt_preamble="test",
            )
            state = GatewayState(config.state_file)
            stdout = asyncio.StreamReader()
            stdout.feed_data(
                b'{"type":"thread.started","thread_id":"019threadstarted"}\n'
            )
            stdout.feed_eof()
            stderr = asyncio.StreamReader()
            stderr.feed_eof()

            fake_process = MagicMock()
            fake_process.pid = 4321
            fake_process.stdout = stdout
            fake_process.stderr = stderr
            fake_process.wait = AsyncMock(return_value=0)

            with patch(
                "codex_gateway.runner.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=fake_process),
            ):
                summary = await run_codex(
                    state=state,
                    config=config,
                    requester_user_id=3,
                    requester_name="tester",
                    prompt="hello",
                    start_new_session=True,
                )

        self.assertEqual(summary.codex_thread_ref, "019threadstarted")

    async def test_run_codex_writes_debug_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = GatewayConfig(
                discord_gateway_token="token",
                control_guild_id=1,
                control_channel_id=2,
                allowed_user_ids={3},
                codex_bin="codex",
                codex_cwd=root,
                codex_home_parent=None,
                codex_home_seed_from=None,
                codex_status_home=root,
                prompt_max_chars=4000,
                status_text_max_chars=700,
                stream_tail_chars=2000,
                stop_sigint_grace_seconds=5.0,
                stop_sigterm_grace_seconds=5.0,
                response_preview_chars=20,
                state_file=root / "state.json",
                tmp_dir=root / "tmp",
                last_response_file=root / "tmp" / "last_response.txt",
                prompt_preamble="test",
            )
            state = GatewayState(config.state_file)
            state.select_session("session-123")

            stdout = asyncio.StreamReader()
            stdout.feed_eof()
            stderr = asyncio.StreamReader()
            stderr.feed_eof()

            fake_process = MagicMock()
            fake_process.pid = 4321
            fake_process.stdout = stdout
            fake_process.stderr = stderr
            fake_process.wait = AsyncMock(return_value=0)

            with patch(
                "codex_gateway.runner.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=fake_process),
            ):
                summary = await run_codex(
                    state=state,
                    config=config,
                    requester_user_id=3,
                    requester_name="tester",
                    prompt="hello",
                    project_id="mail",
                    session_id="abc123",
                    session_ref="session-123",
                    start_new_session=False,
                    model_profile="gpt-5.4",
                )

            debug_path = config.tmp_dir / f"{summary.run_id}-debug.json"
            payload = json.loads(debug_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["session_ref"], "session-123")
        self.assertEqual(payload["project_id"], "mail")
        self.assertEqual(payload["session_id"], "abc123")
        self.assertEqual(payload["model_profile"], "gpt-5.4")
        self.assertEqual(payload["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
