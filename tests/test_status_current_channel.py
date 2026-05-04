from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

from codex_gateway.bot import GatewayClient
from codex_gateway.config import GatewayConfig
from codex_gateway.storage.project_registry import ProjectDefinition, ProjectRegistry
from codex_gateway.storage.session_store import SessionStore
from codex_gateway.state import GatewayState, LastRunSummary


CONTROL_GUILD = 1
CONTROL_CHANNEL = 2
ALLOWED = 3
MAIL_CHANNEL = 5001
OTHER_CHANNEL = 5002


def _make_config(root: Path) -> GatewayConfig:
    return GatewayConfig(
        discord_gateway_token="token",
        control_guild_id=CONTROL_GUILD,
        control_channel_id=CONTROL_CHANNEL,
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
                allowed_model_profiles=["gpt-5.4"],
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


def _seed_session(root: Path, project_id: str, *, label: str) -> str:
    store = SessionStore(root / "state", root / "runtime")
    record = store.create_session(
        project_id=project_id,
        label=label,
        model_profile="gpt-5.4",
        execution_env="openai",
    )
    return record.session_id


def _build(root: Path) -> GatewayClient:
    config = _make_config(root)
    state = GatewayState(config.state_file)
    return GatewayClient(config=config, state=state)


def _interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.channel = MagicMock()
    return interaction


class CurrentChannelTest(unittest.IsolatedAsyncioTestCase):
    async def test_channel_view_uses_channel_project_active_session(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed(root)
            mail_session = _seed_session(root, "mail", label="alerts")

            client = _build(root)
            client._persist_project_active_session("mail", mail_session)
            mail_project = client._load_project_definition("mail")

            # Gateway-global selection is on a different project; channel
            # routing must override that.
            client.state.select_project("other")
            client.state.select_session("ignored-other-session")

            interaction = _interaction()
            await client._handle_current(
                interaction, channel_project=mail_project
            )

            sent = interaction.response.send_message.await_args.args[0]
            self.assertIn("Current selection (channel)", sent)
            self.assertIn("Project: `mail`", sent)
            self.assertIn(mail_session, sent)
            self.assertNotIn("Project: `other`", sent)

    async def test_control_view_keeps_global_selection(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed(root)
            session_id = _seed_session(root, "mail", label="triage")
            client = _build(root)
            client.state.select_project("mail")
            client.state.select_session(session_id)
            interaction = _interaction()

            await client._handle_current(interaction, channel_project=None)

            sent = interaction.response.send_message.await_args.args[0]
            self.assertIn("Current selection (gateway)", sent)
            self.assertIn("Project: `mail`", sent)
            self.assertIn(session_id, sent)


class StatusChannelTest(unittest.IsolatedAsyncioTestCase):
    async def test_channel_view_renders_project_scoped_last_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _seed(root)
            mail_session = _seed_session(root, "mail", label="alerts")
            client = _build(root)
            client._persist_project_active_session("mail", mail_session)
            mail_project = client._load_project_definition("mail")

            now = datetime(2026, 5, 4, 10, 0, tzinfo=timezone.utc)
            mail_summary = LastRunSummary(
                run_id="r-mail",
                requester_user_id=1,
                requester_name="op",
                prompt_excerpt="mail-prompt",
                started_at=now,
                finished_at=now,
                exit_code=0,
                exit_signal=None,
                stdout_excerpt="",
                stderr_excerpt="",
                assistant_response_excerpt="mail-response",
                codex_thread_ref=None,
            )
            other_summary = LastRunSummary(
                run_id="r-other",
                requester_user_id=1,
                requester_name="op",
                prompt_excerpt="other-prompt",
                started_at=now,
                finished_at=now,
                exit_code=0,
                exit_signal=None,
                stdout_excerpt="",
                stderr_excerpt="",
                assistant_response_excerpt="other-response",
                codex_thread_ref=None,
            )
            client.state.project_last_runs["mail"] = mail_summary
            client.state.project_last_runs["other"] = other_summary
            client.state.last_run = other_summary  # global recent is "other"

            interaction = _interaction()
            interaction.followup.send = AsyncMock()
            interaction.response.is_done = MagicMock(return_value=False)

            # Stub the blocking helpers so the test stays purely in-memory.
            async def _passthrough(func, *a, **kw):
                # Mirror _run_blocking: invoke the function in-thread.
                # Default behavior of _has_external_codex_activity is also
                # routed through _run_blocking, so make it short-circuit
                # by name.
                if getattr(func, "__name__", "") == "_has_external_codex_activity":
                    return False
                return func(*a, **kw)

            client._run_blocking = AsyncMock(side_effect=_passthrough)  # type: ignore

            async def _materialize(session):
                return session

            client._materialize_session_record = _materialize  # type: ignore
            await client._handle_status(interaction, channel_project=mail_project)

            sent = "".join(
                str(call.args[0]) if call.args else str(call.kwargs.get("content", ""))
                for call in interaction.followup.send.await_args_list
            )
            # Project-scoped: shows mail's last_run run_id, not other's.
            self.assertIn("r-mail", sent)
            self.assertNotIn("r-other", sent)


if __name__ == "__main__":
    unittest.main()
