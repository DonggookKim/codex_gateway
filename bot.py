from __future__ import annotations

import asyncio
import logging
import os

import discord
from discord import app_commands

from .config import GatewayConfig
from .formatter import (
    format_status,
    limit_discord_message,
)
from .last_response_store import refresh_last_response_file
from .process_inspector import find_codex_processes
from .runner import run_codex, stop_active_run
from .session_inspector import inspect_latest_codex_response
from .state import GatewayState


LOGGER = logging.getLogger(__name__)


class GatewayClient(discord.Client):
    def __init__(self, config: GatewayConfig, state: GatewayState) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.config = config
        self.state = state
        self.tree = app_commands.CommandTree(self)
        self._register_commands()

    def _validate_control_context(self, interaction: discord.Interaction) -> str | None:
        if interaction.guild_id != self.config.control_guild_id:
            return "This command is only available in the configured control guild."
        if interaction.channel_id != self.config.control_channel_id:
            return "This command is only available in the configured control channel."
        if interaction.user.id not in self.config.allowed_user_ids:
            return "You are not allowed to control this Codex gateway."
        return None

    def _register_commands(self) -> None:
        @self.tree.command(
            name="ask",
            description="Send a follow-up prompt to the latest Codex session.",
        )
        @app_commands.describe(prompt="Prompt to send to Codex")
        async def ask(interaction: discord.Interaction, prompt: str) -> None:
            denial = self._validate_control_context(interaction)
            if denial:
                await interaction.response.send_message(denial, ephemeral=True)
                return
            await self._handle_ask(interaction, prompt)

        @self.tree.command(
            name="status",
            description="Show the current gateway state and last run summary.",
        )
        async def status(interaction: discord.Interaction) -> None:
            denial = self._validate_control_context(interaction)
            if denial:
                await interaction.response.send_message(denial, ephemeral=True)
                return
            await self._handle_status(interaction)

        @self.tree.command(
            name="last",
            description="Attach the latest full Codex response as a text file.",
        )
        async def last(interaction: discord.Interaction) -> None:
            denial = self._validate_control_context(interaction)
            if denial:
                await interaction.response.send_message(denial, ephemeral=True)
                return
            await self._handle_last(interaction)

        @self.tree.command(
            name="stop",
            description="Attempt to stop the active gateway-managed Codex run.",
        )
        async def stop(interaction: discord.Interaction) -> None:
            denial = self._validate_control_context(interaction)
            if denial:
                await interaction.response.send_message(denial, ephemeral=True)
                return

            await interaction.response.defer()
            result = await stop_active_run(self.state, self.config)
            await interaction.followup.send(limit_discord_message(result.message))

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.config.control_guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

    async def on_ready(self) -> None:
        LOGGER.info("Gateway connected as %s", self.user)

    async def _safe_defer(self, interaction: discord.Interaction) -> bool:
        try:
            await interaction.response.defer()
            return True
        except discord.NotFound:
            LOGGER.warning("Interaction expired before defer for command response")
            return False

    async def _safe_followup_send(
        self,
        interaction: discord.Interaction,
        content: str,
        *,
        file: discord.File | None = None,
    ) -> bool:
        try:
            kwargs = {"content": content}
            if file is not None:
                kwargs["file"] = file
            await interaction.followup.send(**kwargs)
            return True
        except discord.NotFound:
            LOGGER.warning("Interaction expired before followup send")
            return False

    async def _run_blocking(self, func, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: func(*args))

    def _has_external_codex_activity(self) -> bool:
        exclude_pids = {os.getpid()}
        if self.state.active_run is not None and self.state.active_run.pid is not None:
            exclude_pids.add(self.state.active_run.pid)
        return bool(find_codex_processes(self.config.codex_cwd, exclude_pids))

    async def _handle_ask(
        self,
        interaction: discord.Interaction,
        prompt: str,
    ) -> None:
        cleaned_prompt = prompt.strip()
        if not cleaned_prompt:
            await interaction.response.send_message(
                "Prompt must not be empty.",
                ephemeral=True,
            )
            return
        if len(cleaned_prompt) > self.config.prompt_max_chars:
            await interaction.response.send_message(
                (
                    "Prompt is too long. "
                    f"Max length is {self.config.prompt_max_chars} characters."
                ),
                ephemeral=True,
            )
            return
        if interaction.channel is None:
            await interaction.response.send_message(
                "Control channel is unavailable.",
                ephemeral=True,
            )
            return
        if not await self._safe_defer(interaction):
            return
        if self.state.run_lock.locked():
            await self._safe_followup_send(
                interaction,
                "Gateway is busy with another `/ask` run.",
            )
            return
        if await self._run_blocking(self._has_external_codex_activity):
            await self._safe_followup_send(
                interaction,
                (
                    "Another Codex process appears active in `CODEX_CWD`. "
                    "Gateway `/ask` is blocked to avoid concurrent session writes."
                ),
            )
            return

        await self.state.run_lock.acquire()
        try:
            sent = await self._safe_followup_send(
                interaction,
                limit_discord_message(
                    "Accepted `/ask` request. "
                    "Execution will continue in the latest Codex session on disk."
                )
            )
            if not sent:
                self.state.run_lock.release()
                return
        except Exception:
            self.state.run_lock.release()
            raise

        asyncio.create_task(
            self._run_ask(
                requester_id=interaction.user.id,
                requester_name=interaction.user.display_name,
                prompt=cleaned_prompt,
            )
        )

    async def _handle_status(self, interaction: discord.Interaction) -> None:
        if not await self._safe_defer(interaction):
            return
        external_codex_activity = await self._run_blocking(
            self._has_external_codex_activity
        )
        latest_response = await self._run_blocking(
            inspect_latest_codex_response,
            self.config.codex_status_home,
        )
        await self._run_blocking(
            refresh_last_response_file,
            self.config.last_response_file,
            latest_response,
        )
        await self._safe_followup_send(
            interaction,
            format_status(
                self.state,
                self.config.status_text_max_chars,
                latest_response=latest_response,
                response_preview_chars=self.config.response_preview_chars,
                external_codex_activity=external_codex_activity,
            ),
        )

    async def _handle_last(self, interaction: discord.Interaction) -> None:
        if not await self._safe_defer(interaction):
            return
        latest_response = await self._run_blocking(
            inspect_latest_codex_response,
            self.config.codex_status_home,
        )
        updated = await self._run_blocking(
            refresh_last_response_file,
            self.config.last_response_file,
            latest_response,
        )
        if not updated or not self.config.last_response_file.exists():
            await self._safe_followup_send(
                interaction,
                "No Codex response recorded yet.",
            )
            return

        await self._safe_followup_send(
            interaction,
            "Attached the latest full Codex response.",
            file=discord.File(
                self.config.last_response_file,
                filename="last_response.txt",
            ),
        )

    async def _run_ask(
        self,
        requester_id: int,
        requester_name: str,
        prompt: str,
    ) -> None:
        try:
            await run_codex(
                state=self.state,
                config=self.config,
                requester_user_id=requester_id,
                requester_name=requester_name,
                prompt=prompt,
            )
        except Exception:
            LOGGER.exception("Unhandled exception while running Codex")
        finally:
            if self.state.run_lock.locked():
                self.state.run_lock.release()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = GatewayConfig.from_env()
    state = GatewayState(config.state_file)
    client = GatewayClient(config=config, state=state)
    client.run(config.discord_gateway_token)
