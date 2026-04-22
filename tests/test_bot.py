from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from codex_gateway.bot import GatewayClient
from codex_gateway.config import GatewayConfig
from codex_gateway.session_inspector import LatestCodexResponse
from codex_gateway.state import GatewayState


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
            codex_status_home=root,
            prompt_max_chars=4000,
            status_text_max_chars=700,
            stream_tail_chars=2000,
            stop_sigint_grace_seconds=5.0,
            stop_sigterm_grace_seconds=5.0,
            response_preview_chars=20,
            state_file=root / "gateway_state.json",
            tmp_dir=root / "tmp",
            last_response_file=root / "tmp" / "last_response.txt",
            prompt_preamble="test",
        )
        state = GatewayState(config.state_file)
        return GatewayClient(config=config, state=state)

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
            client = self._make_client(root)
            interaction = MagicMock()
            interaction.user.id = 3
            interaction.user.display_name = "tester"
            interaction.channel = object()
            interaction.response.defer = AsyncMock()
            interaction.followup.send = AsyncMock()

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
