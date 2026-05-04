from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

from codex_gateway.bot import GatewayClient
from codex_gateway.config import GatewayConfig
from codex_gateway.storage.project_registry import ProjectDefinition, ProjectRegistry
from codex_gateway.storage.session_store import SessionStore
from codex_gateway.state import GatewayState


def _make_config(root: Path) -> GatewayConfig:
    return GatewayConfig(
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


def _seed_project(root: Path) -> None:
    registry = ProjectRegistry(root / "state" / "projects.json")
    registry.save_projects(
        [
            ProjectDefinition(
                project_id="mail",
                label="Mail",
                cwd=root,
                project_channel_id=111,
                default_model_profile="gpt-5.4",
                allowed_model_profiles=["gpt-5.4"],
                active_session_id=None,
                archived=False,
            )
        ]
    )


def _make_runtime(*, hostname: str = "127.0.0.1", port: int = 14096, password: str | None = None):
    runtime = MagicMock()
    runtime.settings = MagicMock()
    runtime.settings.server = MagicMock()
    runtime.settings.server.hostname = hostname
    runtime.settings.server.port = port
    runtime.settings.server.password = password
    return runtime


def _interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


def _build_client(root: Path, *, opencode_runtime=None) -> GatewayClient:
    config = _make_config(root)
    state = GatewayState(config.state_file)
    return GatewayClient(config=config, state=state, opencode_runtime=opencode_runtime)


def _seed_session(
    root: Path,
    *,
    backend: str,
    codex_thread_ref: str | None = None,
) -> str:
    store = SessionStore(root / "state", root / "runtime")
    record = store.create_session(
        project_id="mail",
        label="alerts",
        model_profile=("model-connect/Qwen3.5" if backend == "opencode" else "gpt-5.4"),
        execution_env="openai",
        backend=backend,
    )
    if codex_thread_ref is not None:
        store.update_codex_thread_ref("mail", record.session_id, codex_thread_ref)
    return record.session_id


class TuiOpencodeTest(unittest.IsolatedAsyncioTestCase):
    async def test_emits_opencode_attach_command(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed_project(root)
            session_id = _seed_session(
                root,
                backend="opencode",
                codex_thread_ref="ses_abc",
            )
            runtime = _make_runtime()
            client = _build_client(root, opencode_runtime=runtime)
            client.state.select_project("mail")
            client.state.select_session(session_id)

            interaction = _interaction()
            await client._handle_tui(interaction)

            sent = interaction.response.send_message.await_args.args[0]
            self.assertIn(
                "opencode attach http://127.0.0.1:14096 --session ses_abc",
                sent,
            )

    async def test_includes_password_flag_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed_project(root)
            session_id = _seed_session(
                root,
                backend="opencode",
                codex_thread_ref="ses_abc",
            )
            runtime = _make_runtime(password="hunter2")
            client = _build_client(root, opencode_runtime=runtime)
            client.state.select_project("mail")
            client.state.select_session(session_id)

            interaction = _interaction()
            await client._handle_tui(interaction)

            sent = interaction.response.send_message.await_args.args[0]
            self.assertIn("--password 'hunter2'", sent)

    async def test_remote_host_warning(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed_project(root)
            session_id = _seed_session(
                root,
                backend="opencode",
                codex_thread_ref="ses_abc",
            )
            runtime = _make_runtime(hostname="dev.example.com", port=4096)
            client = _build_client(root, opencode_runtime=runtime)
            client.state.select_project("mail")
            client.state.select_session(session_id)

            interaction = _interaction()
            await client._handle_tui(interaction)

            sent = interaction.response.send_message.await_args.args[0]
            self.assertIn("dev.example.com:4096", sent)
            self.assertIn("SSH tunneling", sent)

    async def test_errors_when_runtime_missing(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed_project(root)
            session_id = _seed_session(
                root,
                backend="opencode",
                codex_thread_ref="ses_abc",
            )
            client = _build_client(root, opencode_runtime=None)
            client.state.select_project("mail")
            client.state.select_session(session_id)

            interaction = _interaction()
            await client._handle_tui(interaction)

            sent = interaction.response.send_message.await_args.args[0]
            self.assertIn("OpenCode runtime is not configured", sent)

    async def test_errors_when_session_not_yet_opened(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed_project(root)
            session_id = _seed_session(
                root,
                backend="opencode",
                codex_thread_ref=None,
            )
            runtime = _make_runtime()
            client = _build_client(root, opencode_runtime=runtime)
            client.state.select_project("mail")
            client.state.select_session(session_id)

            interaction = _interaction()
            await client._handle_tui(interaction)

            sent = interaction.response.send_message.await_args.args[0]
            self.assertIn("not been opened yet", sent)


if __name__ == "__main__":
    unittest.main()
