from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

from codex_gateway.bot import GatewayClient
from codex_gateway.config import GatewayConfig
from codex_gateway.storage.project_registry import ProjectDefinition, ProjectRegistry
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


def _seed_project(root: Path, *, default_model: str = "gpt-5.4") -> None:
    registry = ProjectRegistry(root / "state" / "projects.json")
    registry.save_projects(
        [
            ProjectDefinition(
                project_id="mail",
                label="Mail",
                cwd=root,
                project_channel_id=111,
                default_model_profile=default_model,
                allowed_model_profiles=[default_model, "gpt-5.2"],
                active_session_id=None,
                archived=False,
            )
        ]
    )


def _build_client(root: Path, *, opencode_runtime=None) -> GatewayClient:
    config = _make_config(root)
    state = GatewayState(config.state_file)
    return GatewayClient(config=config, state=state, opencode_runtime=opencode_runtime)


def _interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


def _mock_runtime(provider: str = "model-connect", model: str = "Qwen3.5") -> MagicMock:
    runtime = MagicMock()
    runtime.settings = MagicMock()
    runtime.settings.provider_id = provider
    runtime.settings.model_id = model
    return runtime


class SessionNewBackendTest(unittest.IsolatedAsyncioTestCase):
    async def test_default_backend_is_codex(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed_project(root)
            client = _build_client(root)
            client.state.select_project("mail")

            await client._handle_session_new(_interaction(), "triage")

            sid = client.state.selection_state["selected_session_id"]
            assert sid is not None
            session = client.session_store.load_session("mail", sid)
            self.assertEqual(session.backend, "codex")
            # Codex sessions take the project default, not the model_select state.
            self.assertEqual(session.model_profile, "gpt-5.4")

    async def test_codex_backend_ignores_model_select_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed_project(root)
            client = _build_client(root)
            client.state.select_project("mail")
            client.state.set_pending_model_profile("mail", "gpt-5.2")

            await client._handle_session_new(_interaction(), "triage", backend="codex")

            sid = client.state.selection_state["selected_session_id"]
            assert sid is not None
            session = client.session_store.load_session("mail", sid)
            self.assertEqual(session.model_profile, "gpt-5.4")  # not 5.2

    async def test_opencode_backend_uses_runtime_default_when_state_empty(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed_project(root)
            runtime = _mock_runtime()
            client = _build_client(root, opencode_runtime=runtime)
            client.state.select_project("mail")

            await client._handle_session_new(
                _interaction(), "triage", backend="opencode"
            )

            sid = client.state.selection_state["selected_session_id"]
            assert sid is not None
            session = client.session_store.load_session("mail", sid)
            self.assertEqual(session.backend, "opencode")
            self.assertEqual(session.model_profile, "model-connect/Qwen3.5")

    async def test_opencode_backend_prefers_model_select_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed_project(root)
            runtime = _mock_runtime()
            client = _build_client(root, opencode_runtime=runtime)
            client.state.select_project("mail")
            client.state.set_pending_model_profile("mail", "openai/gpt-5.2")

            await client._handle_session_new(
                _interaction(), "triage", backend="opencode"
            )

            sid = client.state.selection_state["selected_session_id"]
            assert sid is not None
            session = client.session_store.load_session("mail", sid)
            self.assertEqual(session.model_profile, "openai/gpt-5.2")

    async def test_opencode_backend_without_runtime_errors(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed_project(root)
            client = _build_client(root, opencode_runtime=None)
            client.state.select_project("mail")
            interaction = _interaction()

            await client._handle_session_new(
                interaction, "triage", backend="opencode"
            )

            self.assertIsNone(client.state.selection_state["selected_session_id"])
            interaction.response.send_message.assert_awaited_once()
            sent = interaction.response.send_message.await_args.args[0]
            self.assertIn("OpenCode backend is not configured", sent)


class ModelSelectScopingTest(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_provider_model_string(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed_project(root)
            client = _build_client(root)
            client.state.select_project("mail")
            interaction = _interaction()

            await client._handle_model_select(
                interaction, "model-connect/Qwen3.5-397B-A17B-FP8"
            )

            self.assertEqual(
                client.state.get_pending_model_profile("mail"),
                "model-connect/Qwen3.5-397B-A17B-FP8",
            )
            sent = interaction.response.send_message.await_args.args[0]
            self.assertIn("opencode", sent.lower())

    async def test_codex_profile_still_accepted_for_legacy_compat(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed_project(root)
            client = _build_client(root)
            client.state.select_project("mail")
            interaction = _interaction()

            await client._handle_model_select(interaction, "gpt-5.2")

            self.assertEqual(
                client.state.get_pending_model_profile("mail"),
                "gpt-5.2",
            )

    async def test_unrecognized_string_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed_project(root)
            client = _build_client(root)
            client.state.select_project("mail")
            interaction = _interaction()

            await client._handle_model_select(interaction, "completely-unknown")

            self.assertIsNone(client.state.get_pending_model_profile("mail"))
            sent = interaction.response.send_message.await_args.args[0]
            self.assertIn("not allowed", sent)


if __name__ == "__main__":
    unittest.main()
