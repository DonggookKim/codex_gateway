from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

from codex_gateway.bot import GatewayClient
from codex_gateway.config import GatewayConfig
from codex_gateway.project_registry import ProjectDefinition, ProjectRegistry
from codex_gateway.state import GatewayState


CONTROL_GUILD_ID = 1
CONTROL_CHANNEL_ID = 2
ALLOWED = 3
MAIL_CHANNEL = 5001
OTHER_CHANNEL = 5002


def _make_config(root: Path) -> GatewayConfig:
    return GatewayConfig(
        discord_gateway_token="token",
        control_guild_id=CONTROL_GUILD_ID,
        control_channel_id=CONTROL_CHANNEL_ID,
        allowed_user_ids={ALLOWED},
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


def _seed(root: Path) -> None:
    registry = ProjectRegistry(root / "state" / "projects.json")
    registry.save_projects(
        [
            ProjectDefinition(
                project_id="mail",
                label="Mail",
                cwd=root,
                project_channel_id=MAIL_CHANNEL,
                default_model_profile="gpt-5.4",
                allowed_model_profiles=["gpt-5.4", "gpt-5.2"],
                active_session_id=None,
                archived=False,
            ),
            ProjectDefinition(
                project_id="other",
                label="Other",
                cwd=root,
                project_channel_id=OTHER_CHANNEL,
                default_model_profile="gpt-5.4",
                allowed_model_profiles=["gpt-5.4"],
                active_session_id=None,
                archived=False,
            ),
        ]
    )


def _build(root: Path, *, opencode_runtime=None) -> GatewayClient:
    config = _make_config(root)
    state = GatewayState(config.state_file)
    return GatewayClient(config=config, state=state, opencode_runtime=opencode_runtime)


def _interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


class SessionNewChannelTest(unittest.IsolatedAsyncioTestCase):
    async def test_channel_create_does_not_touch_global_selection(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed(root)
            client = _build(root)
            client.state.select_project("other")
            client.state.select_session("legacy")
            mail_project = client._load_project_definition("mail")

            await client._handle_session_new(
                _interaction(),
                "alerts",
                backend="codex",
                channel_project=mail_project,
            )

            # Global selection untouched
            self.assertEqual(
                client.state.selection_state["selected_project_id"], "other"
            )
            self.assertEqual(
                client.state.selection_state["selected_session_id"], "legacy"
            )
            # New session is under mail with backend=codex
            mail_after = client._load_project_definition("mail")
            assert mail_after is not None
            assert mail_after.active_session_id is not None
            session = client.session_store.load_session(
                "mail", mail_after.active_session_id
            )
            self.assertEqual(session.backend, "codex")
            self.assertEqual(session.model_profile, "gpt-5.4")

    async def test_control_create_updates_global_and_project(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed(root)
            client = _build(root)
            client.state.select_project("mail")

            await client._handle_session_new(
                _interaction(),
                "triage",
                backend="codex",
                channel_project=None,
            )

            sid = client.state.selection_state["selected_session_id"]
            assert sid is not None
            mail_after = client._load_project_definition("mail")
            assert mail_after is not None
            self.assertEqual(mail_after.active_session_id, sid)


class ModelSelectPerProjectTest(unittest.IsolatedAsyncioTestCase):
    async def test_channel_model_select_scopes_to_channel_project(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed(root)
            client = _build(root)
            # Global selection points at "other" — channel routing must NOT
            # follow that.
            client.state.select_project("other")
            mail_project = client._load_project_definition("mail")

            await client._handle_model_select(
                _interaction(),
                "model-connect/Qwen3.5",
                channel_project=mail_project,
            )

            # mail got the pending value, other did not.
            self.assertEqual(
                client.state.get_pending_model_profile("mail"),
                "model-connect/Qwen3.5",
            )
            self.assertIsNone(client.state.get_pending_model_profile("other"))

    async def test_session_new_for_opencode_reads_per_project_pending(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed(root)

            runtime = MagicMock()
            runtime.settings = MagicMock()
            runtime.settings.provider_id = "default-provider"
            runtime.settings.model_id = "default-model"
            client = _build(root, opencode_runtime=runtime)

            client.state.set_pending_model_profile("mail", "model-connect/Qwen3.5")
            client.state.set_pending_model_profile("other", "openai/gpt-5.2")
            mail_project = client._load_project_definition("mail")

            await client._handle_session_new(
                _interaction(),
                "alerts",
                backend="opencode",
                channel_project=mail_project,
            )

            mail_after = client._load_project_definition("mail")
            assert mail_after is not None
            assert mail_after.active_session_id is not None
            session = client.session_store.load_session(
                "mail", mail_after.active_session_id
            )
            self.assertEqual(session.model_profile, "model-connect/Qwen3.5")

    async def test_session_new_for_opencode_falls_back_to_runtime_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed(root)

            runtime = MagicMock()
            runtime.settings = MagicMock()
            runtime.settings.provider_id = "default-provider"
            runtime.settings.model_id = "default-model"
            client = _build(root, opencode_runtime=runtime)

            mail_project = client._load_project_definition("mail")

            await client._handle_session_new(
                _interaction(),
                "alerts",
                backend="opencode",
                channel_project=mail_project,
            )

            mail_after = client._load_project_definition("mail")
            assert mail_after is not None
            assert mail_after.active_session_id is not None
            session = client.session_store.load_session(
                "mail", mail_after.active_session_id
            )
            self.assertEqual(session.model_profile, "default-provider/default-model")


if __name__ == "__main__":
    unittest.main()
