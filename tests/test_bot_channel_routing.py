from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from codex_gateway.bot import GatewayClient
from codex_gateway.config import GatewayConfig
from codex_gateway.storage.project_registry import ProjectDefinition, ProjectRegistry
from codex_gateway.storage.session_store import SessionStore
from codex_gateway.state import GatewayState


CONTROL_GUILD_ID = 1
CONTROL_CHANNEL_ID = 2
ALLOWED_USER_ID = 3
PROJECT_MAIL_CHANNEL_ID = 5001
PROJECT_OTHER_CHANNEL_ID = 5002


def _build_client(root: Path) -> GatewayClient:
    config = GatewayConfig(
        discord_gateway_token="token",
        control_guild_id=CONTROL_GUILD_ID,
        control_channel_id=CONTROL_CHANNEL_ID,
        allowed_user_ids={ALLOWED_USER_ID},
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


def _seed_two_projects(root: Path) -> None:
    registry = ProjectRegistry(root / "state" / "projects.json")
    registry.save_projects(
        [
            ProjectDefinition(
                project_id="mail",
                label="Mail",
                cwd=root,
                project_channel_id=PROJECT_MAIL_CHANNEL_ID,
                default_model_profile="gpt-5.4",
                allowed_model_profiles=["gpt-5.4"],
                active_session_id=None,
                archived=False,
            ),
            ProjectDefinition(
                project_id="other",
                label="Other",
                cwd=root,
                project_channel_id=PROJECT_OTHER_CHANNEL_ID,
                default_model_profile="gpt-5.4",
                allowed_model_profiles=["gpt-5.4"],
                active_session_id=None,
                archived=False,
            ),
        ]
    )


def _seed_session(
    root: Path,
    project_id: str,
    label: str = "triage",
) -> str:
    store = SessionStore(root / "state", root / "runtime")
    session = store.create_session(
        project_id=project_id,
        label=label,
        model_profile="gpt-5.4",
        execution_env="openai",
    )
    return session.session_id


def _make_interaction(channel_id: int, user_id: int = ALLOWED_USER_ID) -> MagicMock:
    interaction = MagicMock()
    interaction.guild_id = CONTROL_GUILD_ID
    interaction.channel_id = channel_id
    user = MagicMock()
    user.id = user_id
    interaction.user = user
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.channel = MagicMock()
    return interaction


class ValidateCommandContextTest(unittest.TestCase):
    def test_control_channel_returns_no_project(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed_two_projects(root)
            client = _build_client(root)
            interaction = _make_interaction(CONTROL_CHANNEL_ID)
            denial, project = client._validate_command_context(interaction)
        self.assertIsNone(denial)
        self.assertIsNone(project)

    def test_project_channel_returns_project(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed_two_projects(root)
            client = _build_client(root)
            interaction = _make_interaction(PROJECT_MAIL_CHANNEL_ID)
            denial, project = client._validate_command_context(interaction)
        self.assertIsNone(denial)
        self.assertIsNotNone(project)
        assert project is not None
        self.assertEqual(project.project_id, "mail")

    def test_unknown_channel_denies(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed_two_projects(root)
            client = _build_client(root)
            interaction = _make_interaction(99999)
            denial, project = client._validate_command_context(interaction)
        self.assertIsNotNone(denial)
        self.assertIsNone(project)

    def test_disallowed_user_denied_even_in_control(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed_two_projects(root)
            client = _build_client(root)
            interaction = _make_interaction(CONTROL_CHANNEL_ID, user_id=99)
            denial, project = client._validate_command_context(interaction)
        self.assertIsNotNone(denial)
        self.assertIsNone(project)


class SessionSelectChannelTest(unittest.IsolatedAsyncioTestCase):
    async def test_channel_select_does_not_set_global_selection(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed_two_projects(root)
            session_id = _seed_session(root, "mail", "alerts")

            client = _build_client(root)
            client.state.select_project("other")
            client.state.select_session("legacy-session-id")

            interaction = _make_interaction(PROJECT_MAIL_CHANNEL_ID)
            mail_project = client._load_project_definition("mail")

            await client._handle_session_select(
                interaction,
                session_id,
                channel_project=mail_project,
            )

            # Global selection was NOT touched
            self.assertEqual(
                client.state.selection_state["selected_project_id"], "other"
            )
            self.assertEqual(
                client.state.selection_state["selected_session_id"],
                "legacy-session-id",
            )
            # But mail project's active_session_id WAS updated
            mail_after = client._load_project_definition("mail")
            assert mail_after is not None
            self.assertEqual(mail_after.active_session_id, session_id)

    async def test_control_select_updates_global_and_project(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed_two_projects(root)
            session_id = _seed_session(root, "mail")
            client = _build_client(root)
            client.state.select_project("mail")
            interaction = _make_interaction(CONTROL_CHANNEL_ID)

            await client._handle_session_select(
                interaction,
                session_id,
                channel_project=None,
            )

            self.assertEqual(
                client.state.selection_state["selected_session_id"],
                session_id,
            )
            mail_after = client._load_project_definition("mail")
            assert mail_after is not None
            self.assertEqual(mail_after.active_session_id, session_id)


class SessionListChannelTest(unittest.IsolatedAsyncioTestCase):
    async def test_lists_channel_project_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed_two_projects(root)
            mail_session = _seed_session(root, "mail", "mail-task")
            other_session = _seed_session(root, "other", "other-task")
            client = _build_client(root)
            mail_project = client._load_project_definition("mail")
            interaction = _make_interaction(PROJECT_MAIL_CHANNEL_ID)

            await client._handle_session_list(
                interaction,
                channel_project=mail_project,
            )

        interaction.response.send_message.assert_awaited_once()
        sent = interaction.response.send_message.await_args.args[0]
        self.assertIn(mail_session, sent)
        self.assertNotIn(other_session, sent)
        self.assertIn("mail-task", sent)


class AskChannelRoutingTest(unittest.IsolatedAsyncioTestCase):
    async def test_ask_in_project_channel_uses_active_session(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed_two_projects(root)
            session_id = _seed_session(root, "mail")
            client = _build_client(root)
            client._persist_project_active_session("mail", session_id)
            mail_project = client._load_project_definition("mail")
            interaction = _make_interaction(PROJECT_MAIL_CHANNEL_ID)

            with patch.object(
                client,
                "_run_ask",
                new=AsyncMock(return_value=None),
            ), patch.object(
                client,
                "_run_blocking",
                new=AsyncMock(return_value=False),
            ):
                await client._handle_ask(
                    interaction,
                    "do the thing",
                    channel_project=mail_project,
                )

            # The "Accepted" followup should mention the resolved target.
            interaction.followup.send.assert_awaited()
            followups = "".join(
                str(call.args[0]) if call.args else str(call.kwargs.get("content", ""))
                for call in interaction.followup.send.await_args_list
            )
            self.assertIn(session_id, followups)
            self.assertIn("mail", followups)

    async def test_ask_in_project_channel_without_active_session_errors(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed_two_projects(root)
            client = _build_client(root)
            mail_project = client._load_project_definition("mail")
            interaction = _make_interaction(PROJECT_MAIL_CHANNEL_ID)

            await client._handle_ask(
                interaction,
                "do the thing",
                channel_project=mail_project,
            )

            interaction.response.send_message.assert_awaited_once()
            sent = interaction.response.send_message.await_args.args[0]
            self.assertIn("No session selected", sent)


if __name__ == "__main__":
    unittest.main()
