from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from dataclasses import replace
from pathlib import Path

import discord
from discord import app_commands

from .config import GatewayConfig
from .execution_env import infer_execution_env
from .formatter import (
    format_status,
    inline_excerpt,
    limit_discord_message,
    response_preview,
)
from .last_response_store import refresh_last_response_file
from .process_inspector import find_codex_processes
from .project_registry import ProjectDefinition, ProjectRegistry
from .backend import RunRequest
from .backend.opencode_runtime import (
    OpencodeRuntime,
    opencode_runtime_settings_from_env,
)
from .permission_router import PermissionRouter
from .runner import run_codex, stop_active_run
from .session_inspector import inspect_latest_codex_response
from .session_store import SessionRecord, SessionStore
from .state import GatewayState
from .tui_attach import TuiAttachError, build_attach_command, resolve_attach_target


LOGGER = logging.getLogger(__name__)


class GatewayClient(discord.Client):
    def __init__(
        self,
        config: GatewayConfig,
        state: GatewayState,
        opencode_runtime: OpencodeRuntime | None = None,
    ) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.config = config
        self.state = state
        self.project_registry = ProjectRegistry(config.projects_file)
        self.session_store = SessionStore(
            config.state_root,
            config.runtime_root,
        )
        self.opencode_runtime = opencode_runtime
        self.permission_router: PermissionRouter | None = None
        self.tree = app_commands.CommandTree(self)
        self._scheduled_watch_task: asyncio.Task | None = None
        self._register_commands()

    def _validate_control_context(self, interaction: discord.Interaction) -> str | None:
        if interaction.guild_id != self.config.control_guild_id:
            return "This command is only available in the configured control guild."
        if interaction.channel_id != self.config.control_channel_id:
            return "This command is only available in the configured control channel."
        if interaction.user.id not in self.config.allowed_user_ids:
            return "You are not allowed to control this Codex gateway."
        return None

    def _resolve_project_for_channel(
        self,
        channel_id: int | None,
    ) -> ProjectDefinition | None:
        if channel_id is None:
            return None
        for project in self.project_registry.load_projects():
            if project.archived:
                continue
            if project.project_channel_id == channel_id:
                return project
        return None

    def _validate_command_context(
        self,
        interaction: discord.Interaction,
    ) -> tuple[str | None, ProjectDefinition | None]:
        """Allow the command in the control channel OR in any project channel.

        Returns (denial_message, channel_project). When `channel_project` is
        not None the command was invoked in that project's bound channel and
        handlers should treat that project as the implicit target. When None
        and denial_message is None, the command is in the control channel and
        handlers should fall back to the gateway-global selection state.
        """
        if interaction.guild_id != self.config.control_guild_id:
            return (
                "This command is only available in the configured control guild.",
                None,
            )
        if interaction.user.id not in self.config.allowed_user_ids:
            return ("You are not allowed to control this Codex gateway.", None)
        if interaction.channel_id == self.config.control_channel_id:
            return (None, None)
        channel_project = self._resolve_project_for_channel(interaction.channel_id)
        if channel_project is None:
            return (
                "This command must be used in the control channel or a registered project channel.",
                None,
            )
        return (None, channel_project)

    def _load_project_definition(self, project_id: str) -> ProjectDefinition | None:
        for project in self.project_registry.load_projects():
            if project.project_id == project_id and not project.archived:
                return project
        return None

    def _load_session_record(
        self,
        project_id: str,
        session_id: str,
    ) -> SessionRecord | None:
        try:
            record = self.session_store.load_session(project_id, session_id)
        except ValueError:
            return None
        if record.archived:
            return None
        return record

    def _list_project_definitions(self) -> list[ProjectDefinition]:
        return [
            project
            for project in self.project_registry.load_projects()
            if not project.archived
        ]

    def _list_session_records(self, project_id: str) -> list[SessionRecord]:
        return [
            session
            for session in self.session_store.list_sessions(project_id)
            if not session.archived
        ]

    def _build_project_choice_name(self, project: ProjectDefinition) -> str:
        label = project.label or project.project_id
        suffix = (
            f" [{project.default_model_profile}]"
            if project.default_model_profile
            else ""
        )
        return inline_excerpt(f"{label} ({project.project_id}){suffix}", 100)

    def _build_session_choice_name(self, session: SessionRecord) -> str:
        preview_source = ""
        if session.last_prompt_excerpt:
            preview_source = f" prompt={session.last_prompt_excerpt}"
        elif session.last_run_summary:
            response_excerpt = session.last_run_summary.get(
                "assistant_response_excerpt", ""
            )
            if isinstance(response_excerpt, str) and response_excerpt.strip():
                preview_source = f" response={response_excerpt}"
        base = (
            f"{session.session_id} - {session.label} "
            f"[{session.model_profile}|{session.status}]"
        )
        return inline_excerpt(base + preview_source, 100)

    async def _project_id_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        needle = current.strip().lower()
        choices: list[app_commands.Choice[str]] = []
        for project in self._list_project_definitions():
            haystacks = [
                project.project_id.lower(),
                (project.label or "").lower(),
            ]
            if needle and not any(needle in haystack for haystack in haystacks):
                continue
            choices.append(
                app_commands.Choice(
                    name=self._build_project_choice_name(project),
                    value=project.project_id,
                )
            )
            if len(choices) >= 25:
                break
        return choices

    async def _session_id_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        # Prefer the project bound to the channel; fall back to the
        # gateway-global selection for control-channel invocations.
        channel_project = self._resolve_project_for_channel(interaction.channel_id)
        if channel_project is not None:
            target_project_id = channel_project.project_id
        else:
            target_project_id = (
                self.state.selection_state.get("selected_project_id") or ""
            ).strip()
        if not target_project_id:
            return []

        needle = current.strip().lower()
        choices: list[app_commands.Choice[str]] = []
        for session in self._list_session_records(target_project_id):
            haystacks = [
                session.session_id.lower(),
                session.label.lower(),
                session.model_profile.lower(),
                (session.last_prompt_excerpt or "").lower(),
            ]
            if needle and not any(needle in haystack for haystack in haystacks):
                continue
            choices.append(
                app_commands.Choice(
                    name=self._build_session_choice_name(session),
                    value=session.session_id,
                )
            )
            if len(choices) >= 25:
                break
        return choices

    def _allowed_model_profiles(self, project: ProjectDefinition) -> list[str]:
        profiles: list[str] = []
        for profile in (
            [project.default_model_profile]
            + project.allowed_model_profiles
        ):
            cleaned = str(profile).strip()
            if cleaned and cleaned not in profiles:
                profiles.append(cleaned)
        return profiles

    def _determine_execution_env(self, model_profile: str) -> str:
        return infer_execution_env(model_profile)

    async def _model_profile_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        del interaction
        selected_project_id = (
            self.state.selection_state.get("selected_project_id") or ""
        ).strip()
        if not selected_project_id:
            return []

        project = self._load_project_definition(selected_project_id)
        if project is None:
            return []

        needle = current.strip().lower()
        choices: list[app_commands.Choice[str]] = []
        for profile in self._allowed_model_profiles(project):
            if needle and needle not in profile.lower():
                continue
            choices.append(app_commands.Choice(name=profile, value=profile))
            if len(choices) >= 25:
                break
        return choices

    def _persist_project_active_session(
        self,
        project_id: str,
        session_id: str | None,
    ) -> None:
        projects = self.project_registry.load_projects()
        updated_projects: list[ProjectDefinition] = []
        for project in projects:
            if project.project_id == project_id:
                updated_projects.append(
                    replace(project, active_session_id=session_id)
                )
            else:
                updated_projects.append(project)
        self.project_registry.save_projects(updated_projects)

    async def _resolve_project_channel_name(
        self,
        project: ProjectDefinition | None,
    ) -> str | None:
        if project is None or not project.project_channel_id:
            return None
        channel = self.get_channel(project.project_channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(project.project_channel_id)
            except (
                AttributeError,
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
            ):
                return None
        channel_name = getattr(channel, "name", None)
        if not channel_name:
            return None
        return str(channel_name)

    def _register_commands(self) -> None:
        @self.tree.command(
            name="project_select",
            description="Select the active project.",
        )
        @app_commands.describe(project_id="Project to control")
        async def project_select(
            interaction: discord.Interaction,
            project_id: str,
        ) -> None:
            denial = self._validate_control_context(interaction)
            if denial:
                await interaction.response.send_message(denial, ephemeral=True)
                return
            await self._handle_project_select(interaction, project_id)
        @project_select.autocomplete("project_id")
        async def project_select_autocomplete(
            interaction: discord.Interaction,
            current: str,
        ) -> list[app_commands.Choice[str]]:
            return await self._project_id_autocomplete(interaction, current)

        @self.tree.command(
            name="project_list",
            description="List registered projects.",
        )
        async def project_list(interaction: discord.Interaction) -> None:
            denial = self._validate_control_context(interaction)
            if denial:
                await interaction.response.send_message(denial, ephemeral=True)
                return
            await self._handle_project_list(interaction)

        @self.tree.command(
            name="session_select",
            description="Select the active session for the current project.",
        )
        @app_commands.describe(session_id="Session to control")
        async def session_select(
            interaction: discord.Interaction,
            session_id: str,
        ) -> None:
            denial, channel_project = self._validate_command_context(interaction)
            if denial:
                await interaction.response.send_message(denial, ephemeral=True)
                return
            await self._handle_session_select(
                interaction,
                session_id,
                channel_project=channel_project,
            )
        @session_select.autocomplete("session_id")
        async def session_select_autocomplete(
            interaction: discord.Interaction,
            current: str,
        ) -> list[app_commands.Choice[str]]:
            return await self._session_id_autocomplete(interaction, current)

        @self.tree.command(
            name="session_new",
            description="Create and select a new session for the current project.",
        )
        @app_commands.describe(
            label="Human-readable label for the new session",
            backend="Execution backend; defaults to codex (gpt). Choose opencode for the new opencode-backed flow.",
        )
        @app_commands.choices(
            backend=[
                app_commands.Choice(name="codex (gpt)", value="codex"),
                app_commands.Choice(name="opencode", value="opencode"),
            ]
        )
        async def session_new(
            interaction: discord.Interaction,
            label: str,
            backend: app_commands.Choice[str] | None = None,
        ) -> None:
            denial, channel_project = self._validate_command_context(interaction)
            if denial:
                await interaction.response.send_message(denial, ephemeral=True)
                return
            backend_value = backend.value if backend is not None else "codex"
            await self._handle_session_new(
                interaction,
                label,
                backend=backend_value,
                channel_project=channel_project,
            )

        @self.tree.command(
            name="session_list",
            description="List sessions for the project bound to this channel (or the selected one in control).",
        )
        async def session_list(interaction: discord.Interaction) -> None:
            denial, channel_project = self._validate_command_context(interaction)
            if denial:
                await interaction.response.send_message(denial, ephemeral=True)
                return
            await self._handle_session_list(
                interaction,
                channel_project=channel_project,
            )

        @self.tree.command(
            name="model_select",
            description="Pick the provider/model for the next opencode-backed session in this project.",
        )
        @app_commands.describe(
            model_profile="provider/model string (e.g. model-connect/Qwen3.5-...). Codex sessions ignore this.",
        )
        async def model_select(
            interaction: discord.Interaction,
            model_profile: str,
        ) -> None:
            denial, channel_project = self._validate_command_context(interaction)
            if denial:
                await interaction.response.send_message(denial, ephemeral=True)
                return
            await self._handle_model_select(
                interaction,
                model_profile,
                channel_project=channel_project,
            )
        model_select.autocomplete("model_profile")(self._model_profile_autocomplete)

        @self.tree.command(
            name="ask",
            description="Send a follow-up prompt. Targets the project bound to this channel, or the selected one in control.",
        )
        @app_commands.describe(prompt="Prompt to send to Codex")
        async def ask(interaction: discord.Interaction, prompt: str) -> None:
            denial, channel_project = self._validate_command_context(interaction)
            if denial:
                await interaction.response.send_message(denial, ephemeral=True)
                return
            await self._handle_ask(
                interaction,
                prompt,
                channel_project=channel_project,
            )

        @self.tree.command(
            name="status",
            description="Show the gateway state and last run summary (project-scoped when used in a project channel).",
        )
        async def status(interaction: discord.Interaction) -> None:
            denial, channel_project = self._validate_command_context(interaction)
            if denial:
                await interaction.response.send_message(denial, ephemeral=True)
                return
            await self._handle_status(
                interaction,
                channel_project=channel_project,
            )

        @self.tree.command(
            name="current",
            description="Show the active project/session (channel-scoped in a project channel).",
        )
        async def current(interaction: discord.Interaction) -> None:
            denial, channel_project = self._validate_command_context(interaction)
            if denial:
                await interaction.response.send_message(denial, ephemeral=True)
                return
            await self._handle_current(
                interaction,
                channel_project=channel_project,
            )

        @self.tree.command(
            name="tui",
            description="Show the local command to attach a TUI to the selected session.",
        )
        async def tui(interaction: discord.Interaction) -> None:
            denial = self._validate_control_context(interaction)
            if denial:
                await interaction.response.send_message(denial, ephemeral=True)
                return
            await self._handle_tui(interaction)

        @self.tree.command(
            name="watch",
            description="Enable or disable completion watch for the selected session.",
        )
        @app_commands.describe(action="Use `on` or `off`", interval="Optional interval like `30s` or `1m`")
        async def watch(
            interaction: discord.Interaction,
            action: str,
            interval: str = "",
        ) -> None:
            denial = self._validate_control_context(interaction)
            if denial:
                await interaction.response.send_message(denial, ephemeral=True)
                return
            await self._handle_watch(interaction, action, interval)

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

        @self.tree.command(
            name="perms",
            description="List pending opencode permission asks for the channel's project (or selected one in control).",
        )
        async def perms(interaction: discord.Interaction) -> None:
            denial, channel_project = self._validate_command_context(interaction)
            if denial:
                await interaction.response.send_message(denial, ephemeral=True)
                return
            await self._handle_perms(
                interaction,
                channel_project=channel_project,
            )

        @self.tree.command(
            name="perm_allow",
            description="Approve a pending opencode permission ask.",
        )
        @app_commands.describe(
            permission_id="permission id (per_...); leave blank to use the latest pending for the active session",
            scope="`once` (default) or `always`",
        )
        async def perm_allow(
            interaction: discord.Interaction,
            permission_id: str = "",
            scope: str = "once",
        ) -> None:
            denial, channel_project = self._validate_command_context(interaction)
            if denial:
                await interaction.response.send_message(denial, ephemeral=True)
                return
            await self._handle_perm_response(
                interaction,
                permission_id=permission_id.strip(),
                response=scope.strip().lower() or "once",
                channel_project=channel_project,
            )

        @self.tree.command(
            name="perm_reject",
            description="Reject a pending opencode permission ask.",
        )
        @app_commands.describe(
            permission_id="permission id (per_...); leave blank to use the latest pending for the active session",
        )
        async def perm_reject(
            interaction: discord.Interaction,
            permission_id: str = "",
        ) -> None:
            denial, channel_project = self._validate_command_context(interaction)
            if denial:
                await interaction.response.send_message(denial, ephemeral=True)
                return
            await self._handle_perm_response(
                interaction,
                permission_id=permission_id.strip(),
                response="reject",
                channel_project=channel_project,
            )

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.config.control_guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

    async def on_ready(self) -> None:
        LOGGER.info("Gateway connected as %s", self.user)

    async def close(self) -> None:
        if self._scheduled_watch_task is not None:
            self._scheduled_watch_task.cancel()
            try:
                await self._scheduled_watch_task
            except asyncio.CancelledError:
                pass
        await super().close()

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

    async def _handle_project_select(
        self,
        interaction: discord.Interaction,
        project_id: str,
    ) -> None:
        cleaned_project_id = project_id.strip()
        if not cleaned_project_id:
            await interaction.response.send_message(
                "Project ID must not be empty.",
                ephemeral=True,
            )
            return

        try:
            project = self._load_project_definition(cleaned_project_id)
        except ValueError:
            LOGGER.exception("Failed to load project registry")
            await interaction.response.send_message(
                "Project registry is unavailable. Check gateway state on disk.",
                ephemeral=True,
            )
            return

        if project is None:
            await interaction.response.send_message(
                f"Unknown project: `{cleaned_project_id}`",
                ephemeral=True,
            )
            return

        self.state.select_project(project.project_id)
        # /model_select is now a per-project pending value resolved at
        # /session_new time; no longer prefilled here, since the project
        # default is only meaningful for codex sessions which read it
        # directly from ProjectDefinition.default_model_profile.
        if not self.state.selection_state.get("watched_project_id"):
            self._cancel_scheduled_watch()
        await interaction.response.send_message(
            limit_discord_message(
                "Selected project: "
                f"`{project.project_id}`"
                + (
                    f" ({project.label})"
                    if project.label and project.label != project.project_id
                    else ""
                )
                + "\n"
                + "Default model for new sessions: "
                + (
                    f"`{project.default_model_profile}`"
                    if project.default_model_profile
                    else "`unset`"
                )
            ),
            ephemeral=False,
        )

    async def _handle_project_list(self, interaction: discord.Interaction) -> None:
        try:
            projects = self._list_project_definitions()
        except ValueError:
            LOGGER.exception("Failed to load project registry")
            await interaction.response.send_message(
                "Project registry is unavailable. Check gateway state on disk.",
                ephemeral=True,
            )
            return

        if not projects:
            await interaction.response.send_message(
                "No registered projects found.",
                ephemeral=True,
            )
            return

        lines = ["Registered projects:"]
        for project in projects[:25]:
            lines.append(
                f"- `{project.project_id}`"
                + (f" ({project.label})" if project.label else "")
                + (
                    f" channel=`{project.project_channel_id}`"
                    if project.project_channel_id
                    else ""
                )
                + (
                    f" default_model=`{project.default_model_profile}`"
                    if project.default_model_profile
                    else ""
                )
            )
        await interaction.response.send_message(
            limit_discord_message("\n".join(lines)),
            ephemeral=False,
        )

    async def _handle_session_select(
        self,
        interaction: discord.Interaction,
        session_id: str,
        *,
        channel_project: ProjectDefinition | None = None,
    ) -> None:
        # In a project channel: route to that project. In control: fall back
        # to the gateway-global selection.
        if channel_project is not None:
            target_project_id = channel_project.project_id
        else:
            target_project_id = (
                self.state.selection_state.get("selected_project_id") or ""
            ).strip()
        if not target_project_id:
            await interaction.response.send_message(
                "Select a project first with `/project_select`, or invoke this in a project channel.",
                ephemeral=True,
            )
            return

        cleaned_session_id = session_id.strip()
        if not cleaned_session_id:
            await interaction.response.send_message(
                "Session ID must not be empty.",
                ephemeral=True,
            )
            return

        try:
            session = self._load_session_record(
                target_project_id,
                cleaned_session_id,
            )
        except ValueError:
            session = None

        if session is None:
            await interaction.response.send_message(
                (
                    "Unknown session for project "
                    f"`{target_project_id}`: `{cleaned_session_id}`"
                ),
                ephemeral=True,
            )
            return

        # In control channel updates also flip the gateway-global selection
        # so subsequent control-channel commands see it. In a project
        # channel only the project-local active_session_id is updated, so
        # other channels are not disturbed.
        if channel_project is None:
            self.state.select_session(session.session_id)
            if not self.state.selection_state.get("watched_session_id"):
                self._cancel_scheduled_watch()
        try:
            self._persist_project_active_session(
                target_project_id,
                session.session_id,
            )
        except ValueError:
            LOGGER.exception("Failed to persist project active session")
        session_label = session.label or session.session_id
        scope_hint = "channel" if channel_project is not None else "gateway"
        await interaction.response.send_message(
            limit_discord_message(
                f"Selected session ({scope_hint}): "
                f"`{target_project_id}/{session.session_id}`"
                + (
                    f" ({session_label})"
                    if session_label != session.session_id
                    else ""
                )
            ),
            ephemeral=False,
        )

    async def _handle_session_list(
        self,
        interaction: discord.Interaction,
        *,
        channel_project: ProjectDefinition | None = None,
    ) -> None:
        if channel_project is not None:
            target_project_id = channel_project.project_id
        else:
            target_project_id = (
                self.state.selection_state.get("selected_project_id") or ""
            ).strip()
        if not target_project_id:
            await interaction.response.send_message(
                "Select a project first with `/project_select`, or invoke this in a project channel.",
                ephemeral=True,
            )
            return

        sessions = self._list_session_records(target_project_id)
        if not sessions:
            await interaction.response.send_message(
                f"No sessions found for `{target_project_id}`.",
                ephemeral=True,
            )
            return

        lines = [f"Sessions for `{target_project_id}`:"]
        for session in sessions[:25]:
            preview = ""
            if session.last_prompt_excerpt:
                preview = f" prompt={inline_excerpt(session.last_prompt_excerpt, 40)}"
            elif session.last_run_summary:
                response_excerpt = session.last_run_summary.get(
                    "assistant_response_excerpt", ""
                )
                if isinstance(response_excerpt, str) and response_excerpt.strip():
                    preview = f" response={response_preview(response_excerpt, 40)}"
            lines.append(
                f"- `{session.session_id}` ({session.label}) "
                f"model=`{session.model_profile}` status=`{session.status}`{preview}"
            )
        await interaction.response.send_message(
            limit_discord_message("\n".join(lines)),
            ephemeral=False,
        )

    async def _handle_session_new(
        self,
        interaction: discord.Interaction,
        label: str,
        *,
        backend: str = "codex",
        channel_project: ProjectDefinition | None = None,
    ) -> None:
        # Channel-routed creates target the channel's project; control-channel
        # creates target the gateway-globally-selected project.
        if channel_project is not None:
            target_project_id = channel_project.project_id
            project = channel_project
        else:
            target_project_id = (
                self.state.selection_state.get("selected_project_id") or ""
            ).strip()
            if not target_project_id:
                await interaction.response.send_message(
                    "Select a project first with `/project_select`, or invoke this in a project channel.",
                    ephemeral=True,
                )
                return
            project = self._load_project_definition(target_project_id)
            if project is None:
                await interaction.response.send_message(
                    f"Selected project is unavailable: `{target_project_id}`",
                    ephemeral=True,
                )
                return

        cleaned_label = label.strip()
        if not cleaned_label:
            await interaction.response.send_message(
                "Session label must not be empty.",
                ephemeral=True,
            )
            return

        if backend == "opencode":
            if self.opencode_runtime is None:
                await interaction.response.send_message(
                    "OpenCode backend is not configured on this gateway. "
                    "Set OPENCODE_GATEWAY_ENABLED=1 with provider/model env vars.",
                    ephemeral=True,
                )
                return
            model_profile = (
                self.state.get_pending_model_profile(target_project_id)
                or self._opencode_default_model_profile()
            )
        elif backend == "codex":
            # Codex sessions intentionally ignore /model_select; the project
            # default is the source of truth for model bound at creation.
            model_profile = project.default_model_profile
        else:
            await interaction.response.send_message(
                f"Unknown backend: `{backend}` (expected `codex` or `opencode`).",
                ephemeral=True,
            )
            return

        if not model_profile:
            await interaction.response.send_message(
                (
                    f"No model is available for new `{backend}` sessions in "
                    f"`{target_project_id}`."
                    + (
                        " Set `default_model_profile` on the project."
                        if backend == "codex"
                        else " Use `/model_select` or configure OPENCODE_PROVIDER_ID/MODEL_ID."
                    )
                ),
                ephemeral=True,
            )
            return

        session = self.session_store.create_session(
            project_id=target_project_id,
            label=cleaned_label,
            model_profile=model_profile,
            execution_env=self._determine_execution_env(model_profile),
            backend=backend,
        )
        # Channel-routed creates only update the project's active_session_id.
        # Control-channel creates also flip the gateway-global selection.
        if channel_project is None:
            self.state.select_session(session.session_id)
        self._persist_project_active_session(
            target_project_id,
            session.session_id,
        )
        scope_hint = "channel" if channel_project is not None else "gateway"
        await interaction.response.send_message(
            limit_discord_message(
                f"Created session ({scope_hint}): "
                f"`{target_project_id}/{session.session_id}`"
                f" ({cleaned_label}) backend=`{backend}` model=`{model_profile}`"
            ),
            ephemeral=False,
        )

    def _opencode_default_model_profile(self) -> str | None:
        if self.opencode_runtime is None:
            return None
        s = self.opencode_runtime.settings
        return f"{s.provider_id}/{s.model_id}"

    async def _handle_model_select(
        self,
        interaction: discord.Interaction,
        model_profile: str,
        *,
        channel_project: ProjectDefinition | None = None,
    ) -> None:
        if channel_project is not None:
            target_project_id = channel_project.project_id
            project = channel_project
        else:
            target_project_id = (
                self.state.selection_state.get("selected_project_id") or ""
            ).strip()
            if not target_project_id:
                await interaction.response.send_message(
                    "Select a project first with `/project_select`, or invoke this in a project channel.",
                    ephemeral=True,
                )
                return
            try:
                project = self._load_project_definition(target_project_id)
            except ValueError:
                LOGGER.exception("Failed to load project registry")
                project = None
            if project is None:
                await interaction.response.send_message(
                    f"Selected project is unavailable: `{target_project_id}`",
                    ephemeral=True,
                )
                return

        cleaned_model_profile = model_profile.strip()
        if not cleaned_model_profile:
            await interaction.response.send_message(
                "Model profile must not be empty.",
                ephemeral=True,
            )
            return

        # /model_select is now scoped to opencode session creation per
        # project. Accept either a project-allowed codex profile (legacy
        # compat, harmless because codex sessions ignore the pending value)
        # or a free-form provider/model string for opencode.
        allowed_model_profiles = self._allowed_model_profiles(project)
        looks_like_opencode = "/" in cleaned_model_profile
        if (
            allowed_model_profiles
            and cleaned_model_profile not in allowed_model_profiles
            and not looks_like_opencode
        ):
            allowed = ", ".join(
                f"`{profile}`" for profile in allowed_model_profiles
            )
            await interaction.response.send_message(
                (
                    f"Model `{cleaned_model_profile}` is not allowed for "
                    f"`{target_project_id}` and does not look like an "
                    f"opencode `provider/model` string. Allowed codex profiles: "
                    f"{allowed}"
                ),
                ephemeral=True,
            )
            return

        self.state.set_pending_model_profile(
            target_project_id,
            cleaned_model_profile,
        )
        scope_note = (
            "(applies to the next opencode session creation in this project)"
            if looks_like_opencode
            else "(applies only when /session_new is run with backend=opencode in this project)"
        )
        await interaction.response.send_message(
            limit_discord_message(
                f"Selected model for new sessions in "
                f"`{target_project_id}`: `{cleaned_model_profile}` {scope_note}"
            ),
            ephemeral=False,
        )

    async def _handle_watch(
        self,
        interaction: discord.Interaction,
        action: str,
        interval: str | None,
    ) -> None:
        cleaned_action = action.strip().lower()
        if cleaned_action not in {"on", "off"}:
            await interaction.response.send_message(
                "Watch action must be `on` or `off`.",
                ephemeral=True,
            )
            return
        if cleaned_action == "off":
            self._cancel_scheduled_watch()
            self.state.disable_watch()
            await interaction.response.send_message(
                "Watch disabled.",
                ephemeral=False,
            )
            return

        selected_project_id = (
            self.state.selection_state.get("selected_project_id") or ""
        ).strip()
        selected_session_id = (
            self.state.selection_state.get("selected_session_id") or ""
        ).strip()
        if not selected_project_id or not selected_session_id:
            await interaction.response.send_message(
                "Select both a project and a session before enabling watch.",
                ephemeral=True,
            )
            return

        try:
            interval_seconds = self._parse_watch_interval(interval)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        run_id: str | None = None
        if (
            self.state.active_run is not None
            and self.state.active_run.project_id == selected_project_id
            and self.state.active_run.session_id == selected_session_id
        ):
            run_id = self.state.active_run.run_id
        self.state.enable_watch(
            selected_project_id,
            selected_session_id,
            run_id=run_id,
            interval_seconds=interval_seconds,
        )
        await interaction.response.send_message(
            limit_discord_message(
                "Watch enabled for "
                f"`{selected_project_id}/{selected_session_id}`. "
                f"Next `/ask` will schedule repeated checks every `{self._format_watch_interval(interval_seconds)}` until idle. "
                "If the selected project or session changes, watch turns off automatically."
            ),
            ephemeral=False,
        )

    async def _handle_tui(self, interaction: discord.Interaction) -> None:
        selected_project_id = (
            self.state.selection_state.get("selected_project_id") or ""
        ).strip()
        selected_session_id = (
            self.state.selection_state.get("selected_session_id") or ""
        ).strip()
        if not selected_project_id or not selected_session_id:
            await interaction.response.send_message(
                "Select both a project and a session before using `/tui`.",
                ephemeral=True,
            )
            return

        session = self._load_session_record(selected_project_id, selected_session_id)
        if session is None:
            await interaction.response.send_message(
                (
                    "Selected session is unavailable for project "
                    f"`{selected_project_id}`: `{selected_session_id}`"
                ),
                ephemeral=True,
            )
            return

        if session.backend == "opencode":
            await self._reply_opencode_tui(interaction, session)
            return

        session = await self._materialize_session_record(session)
        try:
            attach_target = resolve_attach_target(
                state_root=self.config.state_root,
                projects_file=self.config.projects_file,
                project_id=selected_project_id,
                session_id=selected_session_id,
            )
            command = build_attach_command(
                repo_root=self.config.codex_cwd,
                project_id=selected_project_id,
                session=session,
            )
        except TuiAttachError as exc:
            await interaction.response.send_message(
                limit_discord_message(
                    "Cannot attach this session to a local TUI yet.\n"
                    f"Reason: {exc}"
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            limit_discord_message(
                "Run this in a local terminal to attach the selected gateway session:\n"
                f"`{command}`\n"
                f"Resolved HOME: `{attach_target.home_parent}`\n"
                f"Resolved thread: `{attach_target.thread_ref}`"
            ),
            ephemeral=False,
        )

    async def _reply_opencode_tui(
        self,
        interaction: discord.Interaction,
        session: SessionRecord,
    ) -> None:
        if self.opencode_runtime is None:
            await interaction.response.send_message(
                "OpenCode runtime is not configured on this gateway, "
                "so an opencode-backed session cannot be attached. "
                "Set the OPENCODE_GATEWAY_ENABLED env var first.",
                ephemeral=True,
            )
            return
        opencode_session_id = (session.codex_thread_ref or "").strip()
        if not opencode_session_id:
            await interaction.response.send_message(
                "This opencode session has not been opened yet "
                "(no opencode session ID recorded). Send an `/ask` first.",
                ephemeral=True,
            )
            return
        server_settings = self.opencode_runtime.settings.server
        url = f"http://{server_settings.hostname}:{server_settings.port}"
        password_arg = (
            f" --password '{server_settings.password}'"
            if server_settings.password
            else ""
        )
        command = (
            f"opencode attach {url} --session {opencode_session_id}{password_arg}"
        )
        await interaction.response.send_message(
            limit_discord_message(
                "Run this in a local terminal to attach an opencode TUI to "
                "the selected gateway session:\n"
                f"`{command}`\n"
                f"Opencode session: `{opencode_session_id}`"
                + (
                    "\nNote: the local terminal must be able to reach the "
                    f"opencode server at `{url}` (use SSH tunneling for "
                    "remote hosts)."
                    if server_settings.hostname not in {"127.0.0.1", "localhost"}
                    else ""
                )
            ),
            ephemeral=False,
        )

    async def _handle_ask(
        self,
        interaction: discord.Interaction,
        prompt: str,
        *,
        channel_project: ProjectDefinition | None = None,
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
                "Channel is unavailable.",
                ephemeral=True,
            )
            return

        # Resolve project + session: project channel binds the project and
        # uses ProjectDefinition.active_session_id; control channel uses the
        # gateway-global selection.
        if channel_project is not None:
            selected_project_id = channel_project.project_id
            selected_session_id = (channel_project.active_session_id or "").strip()
            project = channel_project
        else:
            selected_project_id = (
                self.state.selection_state.get("selected_project_id") or ""
            ).strip()
            if not selected_project_id:
                await interaction.response.send_message(
                    "Select a project first with `/project_select`, or invoke this in a project channel.",
                    ephemeral=True,
                )
                return
            selected_session_id = (
                self.state.selection_state.get("selected_session_id") or ""
            ).strip()
            try:
                project = self._load_project_definition(selected_project_id)
            except ValueError:
                LOGGER.exception("Failed to load project registry")
                project = None
            if project is None:
                await interaction.response.send_message(
                    f"Selected project is unavailable: `{selected_project_id}`",
                    ephemeral=True,
                )
                return

        if not selected_session_id:
            await interaction.response.send_message(
                (
                    "No session selected for "
                    f"`{selected_project_id}`. "
                    "Use `/session_select` (or `/session_new`) first."
                ),
                ephemeral=True,
            )
            return
        project_channel_name = await self._resolve_project_channel_name(project)

        try:
            session = self._load_session_record(
                selected_project_id,
                selected_session_id,
            )
        except ValueError:
            session = None
        if session is None:
            await interaction.response.send_message(
                (
                    "Selected session is unavailable for project "
                    f"`{selected_project_id}`: `{selected_session_id}`"
                ),
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
            self.session_store.update_prompt_excerpt(
                selected_project_id,
                session.session_id,
                inline_excerpt(cleaned_prompt, 240),
            )
            sent = await self._safe_followup_send(
                interaction,
                limit_discord_message(
                    "Accepted `/ask` request for "
                    f"`{selected_project_id}/{selected_session_id}`.\n"
                    "Prompt: "
                    f"{inline_excerpt(cleaned_prompt, 240)}"
                )
            )
            if not sent:
                self.state.run_lock.release()
                return
        except Exception:
            self.state.run_lock.release()
            raise

        self._schedule_watch_for_ask(selected_project_id, session.session_id)
        asyncio.create_task(
            self._run_ask(
                requester_id=interaction.user.id,
                requester_name=interaction.user.display_name,
                prompt=cleaned_prompt,
                project_id=selected_project_id,
                session_id=session.session_id,
                codex_session_ref=session.codex_thread_ref,
                discord_channel_name=project_channel_name,
                model_profile=session.model_profile,
                project_label=project.label or project.project_id,
                session_label=session.label or session.session_id,
            )
        )

    async def _handle_status(
        self,
        interaction: discord.Interaction,
        *,
        channel_project: ProjectDefinition | None = None,
    ) -> None:
        if not await self._safe_defer(interaction):
            return
        target_session = self._resolve_active_session_for_command(channel_project)
        bound_backend = target_session.backend if target_session else None
        bound_model_profile = (
            target_session.model_profile if target_session else None
        )
        bound_execution_env = (
            target_session.execution_env if target_session else None
        )
        external_codex_activity = await self._run_blocking(
            self._has_external_codex_activity
        )
        latest_response = None
        if target_session is not None:
            target_session = await self._materialize_session_record(target_session)
            latest_response = await self._run_blocking(
                inspect_latest_codex_response,
                target_session.codex_home_path,
            )
            await self._run_blocking(
                refresh_last_response_file,
                target_session.last_response_path,
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
                bound_model_profile=bound_model_profile,
                bound_execution_env=bound_execution_env,
                bound_backend=bound_backend,
                project_scope_id=(
                    channel_project.project_id if channel_project is not None else None
                ),
            ),
        )

    async def _handle_current(
        self,
        interaction: discord.Interaction,
        *,
        channel_project: ProjectDefinition | None = None,
    ) -> None:
        # Channel-routed view shows the channel's project + that project's
        # active_session_id. Control view shows the gateway-globally
        # selected project/session.
        if channel_project is not None:
            target_project_id = channel_project.project_id
            target_session_id = (channel_project.active_session_id or "").strip()
            project = channel_project
        else:
            target_project_id = (
                self.state.selection_state.get("selected_project_id") or ""
            ).strip()
            target_session_id = (
                self.state.selection_state.get("selected_session_id") or ""
            ).strip()
            if not target_project_id:
                await interaction.response.send_message(
                    "No project is currently selected.",
                    ephemeral=True,
                )
                return
            project = self._load_project_definition(target_project_id)

        session = None
        if target_session_id:
            session = self._load_session_record(target_project_id, target_session_id)

        watch_on = (
            self.state.selection_state.get("watched_project_id") == target_project_id
            and self.state.selection_state.get("watched_session_id") == target_session_id
        )
        interval_raw = self.state.selection_state.get("watch_interval_seconds")
        interval_text = ""
        if watch_on and interval_raw is not None:
            try:
                interval_text = self._format_watch_interval(float(interval_raw))
            except ValueError:
                interval_text = str(interval_raw)

        scope_hint = "channel" if channel_project is not None else "gateway"
        lines = [
            f"Current selection ({scope_hint})",
            f"Project: `{target_project_id}`"
            + (
                f" ({project.label})"
                if project is not None and project.label
                else ""
            ),
        ]
        if session is None:
            if target_session_id:
                lines.append(f"Session: `{target_session_id}` (unavailable)")
            else:
                lines.append("Session: `none`")
        else:
            lines.append(
                f"Session: `{session.session_id}` ({session.label}) status=`{session.status}`"
            )
            lines.append(f"Backend: `{session.backend}`")
            lines.append(f"Model: `{session.model_profile}`")
            lines.append(f"Execution env: `{session.execution_env}`")
            if session.codex_thread_ref:
                lines.append(f"Codex thread: `{session.codex_thread_ref}`")

        if watch_on:
            lines.append(
                "Watch: `on`"
                + (f" every `{interval_text}`" if interval_text else "")
            )
        else:
            lines.append("Watch: `off`")

        await interaction.response.send_message(
            limit_discord_message("\n".join(lines)),
            ephemeral=False,
        )

    async def _handle_last(self, interaction: discord.Interaction) -> None:
        if not await self._safe_defer(interaction):
            return
        selected_session = self._selected_session_record()
        if selected_session is None:
            await self._safe_followup_send(
                interaction,
                "Select a project and session before using `/last`.",
            )
            return
        selected_session = await self._materialize_session_record(selected_session)
        latest_response = await self._run_blocking(
            inspect_latest_codex_response,
            selected_session.codex_home_path,
        )
        updated = await self._run_blocking(
            refresh_last_response_file,
            selected_session.last_response_path,
            latest_response,
        )
        if not updated and not selected_session.last_response_path.exists():
            await self._safe_followup_send(
                interaction,
                "No Codex response recorded yet.",
            )
            return

        await self._safe_followup_send(
            interaction,
            "Attached the latest full Codex response.",
            file=discord.File(
                selected_session.last_response_path,
                filename="last_response.txt",
            ),
        )

    async def _run_ask(
        self,
        requester_id: int,
        requester_name: str,
        prompt: str,
        project_id: str | None = None,
        session_id: str | None = None,
        codex_session_ref: str | None = None,
        discord_channel_name: str | None = None,
        model_profile: str | None = None,
        project_label: str | None = None,
        session_label: str | None = None,
    ) -> None:
        try:
            summary = await self._run_codex_attempt(
                requester_id=requester_id,
                requester_name=requester_name,
                prompt=prompt,
                project_id=project_id,
                session_id=session_id,
                codex_session_ref=codex_session_ref,
                discord_channel_name=discord_channel_name,
                model_profile=model_profile,
                project_label=project_label,
                session_label=session_label,
            )
            if self._should_retry_missing_rollout(summary):
                summary = await self._run_codex_attempt(
                    requester_id=requester_id,
                    requester_name=requester_name,
                    prompt=prompt,
                    project_id=project_id,
                    session_id=session_id,
                    codex_session_ref=None,
                    discord_channel_name=discord_channel_name,
                    model_profile=model_profile,
                    project_label=project_label,
                    session_label=session_label,
                )
            if project_id and session_id:
                session_record = self.session_store.update_last_run_summary(
                    project_id,
                    session_id,
                    summary.to_json_dict(),
                )
                self._persist_session_artifacts(session_record, summary.run_id, summary)
                if summary.codex_thread_ref:
                    self.session_store.update_codex_thread_ref(
                        project_id,
                        session_id,
                        summary.codex_thread_ref,
                    )
        except Exception:
            LOGGER.exception("Unhandled exception while running Codex")
        finally:
            if self.state.run_lock.locked():
                self.state.run_lock.release()

    async def _run_codex_attempt(
        self,
        *,
        requester_id: int,
        requester_name: str,
        prompt: str,
        project_id: str | None,
        session_id: str | None,
        codex_session_ref: str | None,
        discord_channel_name: str | None,
        model_profile: str | None,
        project_label: str | None,
        session_label: str | None,
    ) -> LastRunSummary:
        session_record = None
        if project_id and session_id:
            session_record = self._load_session_record(project_id, session_id)
            session_record = await self._materialize_session_record(session_record)

        if session_record is not None and session_record.backend == "opencode":
            return await self._run_opencode_attempt(
                requester_id=requester_id,
                requester_name=requester_name,
                prompt=prompt,
                project_id=project_id,
                session_id=session_id,
                opencode_session_ref=codex_session_ref,
                model_profile=model_profile,
                project_label=project_label,
                session_label=session_label,
            )

        return await run_codex(
            state=self.state,
            config=self.config,
            requester_user_id=requester_id,
            requester_name=requester_name,
            prompt=prompt,
            project_id=project_id,
            session_id=session_id,
            session_ref=codex_session_ref,
            start_new_session=codex_session_ref is None,
            discord_channel_name=discord_channel_name,
            model_profile=model_profile,
            execution_env=(
                session_record.execution_env
                if session_record is not None
                else self._determine_execution_env(model_profile or "")
            ),
            codex_home_path=(
                session_record.codex_home_path if session_record is not None else None
            ),
            runtime_root=(
                session_record.runtime_root if session_record is not None else None
            ),
            last_response_file=(
                session_record.last_response_path
                if session_record is not None
                else None
            ),
            project_label=project_label,
            session_label=session_label,
        )

    async def _run_opencode_attempt(
        self,
        *,
        requester_id: int,
        requester_name: str,
        prompt: str,
        project_id: str | None,
        session_id: str | None,
        opencode_session_ref: str | None,
        model_profile: str | None,
        project_label: str | None,
        session_label: str | None,
    ) -> LastRunSummary:
        if self.opencode_runtime is None:
            raise RuntimeError(
                "OpenCode session selected but no OpencodeRuntime is configured. "
                "Set OPENCODE_GATEWAY_ENABLED=1 with provider/model env vars."
            )
        backend = await self.opencode_runtime.start()
        if self.permission_router is None:
            self.permission_router = PermissionRouter(
                gateway_client=self,
                opencode_client=self.opencode_runtime.client,
                timeout_seconds=self.opencode_runtime.settings.idle_timeout_seconds,
                allowed_user_ids=self.config.allowed_user_ids,
            )
            self.opencode_runtime.set_permission_callback(
                self.permission_router.on_permission_asked
            )
        request = RunRequest(
            requester_user_id=requester_id,
            requester_name=requester_name,
            prompt=prompt,
            project_id=project_id,
            session_id=session_id,
            session_ref=opencode_session_ref,
            start_new_session=opencode_session_ref is None,
            model_profile=model_profile,
            project_label=project_label,
            session_label=session_label,
        )
        return await backend.run(self.state, self.config, request)

    def _resolve_active_session_for_command(
        self,
        channel_project: ProjectDefinition | None,
    ) -> SessionRecord | None:
        """Pick the session a command should act on for the given context.

        Project-channel: that project's `active_session_id`.
        Control-channel: the gateway-globally selected session.
        """
        if channel_project is not None:
            session_id = (channel_project.active_session_id or "").strip()
            if not session_id:
                return None
            return self._load_session_record(channel_project.project_id, session_id)
        return self._selected_session_record()

    async def _handle_perms(
        self,
        interaction: discord.Interaction,
        *,
        channel_project: ProjectDefinition | None = None,
    ) -> None:
        if self.permission_router is None or not self.permission_router.pending:
            await interaction.response.send_message(
                "No pending opencode permission asks.",
                ephemeral=True,
            )
            return

        if channel_project is not None:
            # Filter to opencode sessions known to live under this channel's
            # project.
            project_id = channel_project.project_id
            opencode_session_ids = {
                session.codex_thread_ref
                for session in self._list_session_records(project_id)
                if session.codex_thread_ref
            }
            pending = [
                p
                for p in self.permission_router.list_pending()
                if p.ask.session_id in opencode_session_ids
            ]
            scope_label = f"project `{project_id}`"
        else:
            target_record = self._resolve_active_session_for_command(None)
            opencode_session_id = (
                target_record.codex_thread_ref if target_record is not None else None
            )
            if opencode_session_id:
                pending = self.permission_router.list_pending(opencode_session_id)
                scope_label = (
                    f"selected session `{target_record.session_id}`"
                    if target_record is not None
                    else "selected session"
                )
            else:
                pending = self.permission_router.list_pending()
                scope_label = "all sessions"

        if not pending:
            await interaction.response.send_message(
                f"No pending opencode permission asks for {scope_label}.",
                ephemeral=True,
            )
            return

        lines = [f"Pending permission asks for {scope_label}:"]
        for pending_perm in pending:
            ask = pending_perm.ask
            lines.append(
                f"- `{ask.permission_id}` "
                f"perm=`{ask.permission}` "
                f"patterns={ask.patterns!r} "
                f"opencode-session=`{ask.session_id}`"
            )
        await interaction.response.send_message(
            limit_discord_message("\n".join(lines)),
            ephemeral=True,
        )

    async def _handle_perm_response(
        self,
        interaction: discord.Interaction,
        *,
        permission_id: str,
        response: str,
        channel_project: ProjectDefinition | None = None,
    ) -> None:
        if self.permission_router is None:
            await interaction.response.send_message(
                "Permission router is not active. Run an opencode-backed `/ask` first.",
                ephemeral=True,
            )
            return

        target_id = permission_id
        if not target_id:
            target_record = self._resolve_active_session_for_command(channel_project)
            opencode_session_id = (
                target_record.codex_thread_ref if target_record is not None else None
            )
            if not opencode_session_id:
                hint = (
                    "Either pass a `permission_id`, select a session in this "
                    "channel's project, or invoke from the control channel "
                    "with a session selected."
                )
                await interaction.response.send_message(hint, ephemeral=True)
                return
            latest = self.permission_router.get_latest_pending_for_session(
                opencode_session_id
            )
            if latest is None:
                await interaction.response.send_message(
                    "No pending permission asks for the active session.",
                    ephemeral=True,
                )
                return
            target_id = latest.ask.permission_id

        await interaction.response.defer(ephemeral=True)
        ok, message = await self.permission_router.respond(
            target_id,
            response,
            responder_label=str(interaction.user),
        )
        await interaction.followup.send(message, ephemeral=True)

    def _should_retry_missing_rollout(self, summary: LastRunSummary) -> bool:
        if summary.exit_code == 0:
            return False
        stderr_excerpt = summary.stderr_excerpt
        if not isinstance(stderr_excerpt, str):
            return False
        stderr = stderr_excerpt.lower()
        return "thread/resume failed" in stderr and "no rollout found" in stderr

    def _selected_session_model_profile(self) -> str | None:
        session = self._selected_session_record()
        if session is None:
            return None
        return session.model_profile or None

    def _selected_session_execution_env(self) -> str | None:
        session = self._selected_session_record()
        if session is None:
            return None
        return session.execution_env or None

    def _selected_session_backend(self) -> str | None:
        session = self._selected_session_record()
        if session is None:
            return None
        return session.backend or None

    def _selected_session_record(self) -> SessionRecord | None:
        project_id = (self.state.selection_state.get("selected_project_id") or "").strip()
        session_id = (self.state.selection_state.get("selected_session_id") or "").strip()
        if not project_id or not session_id:
            return None
        return self._load_session_record(project_id, session_id)

    async def _materialize_session_record(
        self,
        session: SessionRecord | None,
    ) -> SessionRecord | None:
        if session is None:
            return None
        return await self._run_blocking(
            self.session_store.materialize_session_codex_home,
            session,
        )

    def _session_stderr_path(self, session: SessionRecord) -> Path:
        return session.last_response_path.with_name("last_stderr.txt")

    def _session_debug_path(self, session: SessionRecord) -> Path:
        return session.last_response_path.with_name("last_debug.json")

    def _cancel_scheduled_watch(self) -> None:
        if self._scheduled_watch_task is not None:
            self._scheduled_watch_task.cancel()
            self._scheduled_watch_task = None

    def _parse_watch_interval(self, raw: str | None) -> float:
        if raw is None or not raw.strip():
            return 600.0
        cleaned = raw.strip().lower()
        match = re.fullmatch(r"(\d+)(s|m|h)", cleaned)
        if match is None:
            raise ValueError("Watch interval must look like `30s`, `1m`, or `2h`.")
        value = int(match.group(1))
        unit = match.group(2)
        multiplier = {"s": 1, "m": 60, "h": 3600}[unit]
        return float(value * multiplier)

    def _format_watch_interval(self, seconds: float) -> str:
        if seconds % 3600 == 0:
            return f"{int(seconds // 3600)}h"
        if seconds % 60 == 0:
            return f"{int(seconds // 60)}m"
        return f"{int(seconds)}s"

    def _schedule_watch_for_ask(self, project_id: str, session_id: str) -> None:
        watched_project_id = (
            self.state.selection_state.get("watched_project_id") or ""
        ).strip()
        watched_session_id = (
            self.state.selection_state.get("watched_session_id") or ""
        ).strip()
        interval_raw = self.state.selection_state.get("watch_interval_seconds")
        if watched_project_id != project_id or watched_session_id != session_id:
            return
        try:
            interval_seconds = float(interval_raw) if interval_raw else 600.0
        except ValueError:
            interval_seconds = 600.0
        self._cancel_scheduled_watch()
        self._scheduled_watch_task = asyncio.create_task(
            self._run_watch_timer(interval_seconds)
        )

    async def _run_watch_timer(self, interval_seconds: float) -> None:
        try:
            keep_checking = True
            while keep_checking:
                await asyncio.sleep(interval_seconds)
                keep_checking = await self._check_watch_once()
        except asyncio.CancelledError:
            raise
        finally:
            self._scheduled_watch_task = None

    def _persist_session_artifacts(
        self,
        session: SessionRecord,
        run_id: str,
        summary,
    ) -> None:
        if (
            not summary.assistant_response_excerpt.strip()
            and session.last_response_path.exists()
        ):
            session.last_response_path.unlink()
        stderr_artifact = session.runtime_root / "tmp" / f"{run_id}-stderr.txt"
        if stderr_artifact.exists() and stderr_artifact.stat().st_size > 0:
            shutil.copyfile(stderr_artifact, self._session_stderr_path(session))
        else:
            stderr_path = self._session_stderr_path(session)
            if stderr_path.exists():
                stderr_path.unlink()
        debug_artifact = session.runtime_root / "tmp" / f"{run_id}-debug.json"
        if debug_artifact.exists() and debug_artifact.stat().st_size > 0:
            shutil.copyfile(debug_artifact, self._session_debug_path(session))
        else:
            debug_path = self._session_debug_path(session)
            if debug_path.exists():
                debug_path.unlink()

    async def _send_project_channel_message(
        self,
        project: ProjectDefinition,
        content: str,
        *,
        file_paths: list[Path] | None = None,
    ) -> bool:
        channel = self.get_channel(project.project_channel_id) if project.project_channel_id else None
        if channel is None and project.project_channel_id:
            try:
                channel = await self.fetch_channel(project.project_channel_id)
            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
            ):
                return False
        if channel is None:
            return False
        files = [
            discord.File(path, filename=path.name)
            for path in (file_paths or [])
            if path.exists()
        ]
        kwargs = {"content": limit_discord_message(content)}
        if files:
            kwargs["files"] = files
        try:
            await channel.send(**kwargs)
        finally:
            for file in files:
                file.close()
        return True

    async def _check_watch_once(self) -> bool:
        watched_project_id = (
            self.state.selection_state.get("watched_project_id") or ""
        ).strip()
        watched_session_id = (
            self.state.selection_state.get("watched_session_id") or ""
        ).strip()
        if not watched_project_id or not watched_session_id:
            return False

        project = self._load_project_definition(watched_project_id)
        session = self._load_session_record(watched_project_id, watched_session_id)
        if project is None or session is None:
            self._cancel_scheduled_watch()
            self.state.disable_watch()
            return False

        active_run = self.state.active_run
        if (
            active_run is not None
            and active_run.project_id == watched_project_id
            and active_run.session_id == watched_session_id
        ):
            session_summary = format_status(
                self.state,
                self.config.status_text_max_chars,
                latest_response=None,
                response_preview_chars=self.config.response_preview_chars,
                external_codex_activity=False,
                bound_model_profile=session.model_profile,
                bound_execution_env=session.execution_env,
            )
            await self._send_project_channel_message(
                project,
                "Watch snapshot for "
                f"`{watched_project_id}/{watched_session_id}`.\n"
                + session_summary,
            )
            return True

        if not session.last_run_summary:
            return False

        exit_code_raw = session.last_run_summary.get("exit_code")
        exit_signal = session.last_run_summary.get("exit_signal")
        stderr_excerpt = str(session.last_run_summary.get("stderr_excerpt", ""))
        response_excerpt = str(
            session.last_run_summary.get("assistant_response_excerpt", "")
        )
        failed = exit_signal is not None or exit_code_raw not in (0, None)
        file_paths: list[Path] = []
        if session.last_response_path.exists():
            file_paths.append(session.last_response_path)
        stderr_path = self._session_stderr_path(session)
        if failed and stderr_path.exists():
            file_paths.append(stderr_path)

        content_lines = [
            "Watch complete for "
            f"`{watched_project_id}/{watched_session_id}`.",
        ]
        if exit_signal:
            content_lines.append(f"Exit signal: `{exit_signal}`")
        elif exit_code_raw is not None:
            content_lines.append(f"Exit code: `{exit_code_raw}`")

        if failed:
            if stderr_excerpt.strip():
                content_lines.append(
                    "stderr: " + inline_excerpt(stderr_excerpt, self.config.status_text_max_chars)
                )
            elif response_excerpt.strip():
                content_lines.append(
                    "response: " + response_preview(
                        response_excerpt,
                        self.config.response_preview_chars,
                    )
                )
        elif response_excerpt.strip():
            content_lines.append(
                "response: " + response_preview(
                    response_excerpt,
                    self.config.response_preview_chars,
                )
            )

        await self._send_project_channel_message(
            project,
            "\n".join(content_lines),
            file_paths=file_paths,
        )
        return False


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = GatewayConfig.from_env()
    state = GatewayState(config.state_file)

    opencode_runtime: OpencodeRuntime | None = None
    runtime_settings = opencode_runtime_settings_from_env()
    if runtime_settings is not None:
        opencode_runtime = OpencodeRuntime(runtime_settings)
        LOGGER.info(
            "OpencodeRuntime enabled: provider=%s model=%s server=%s:%s",
            runtime_settings.provider_id,
            runtime_settings.model_id,
            runtime_settings.server.hostname,
            runtime_settings.server.port,
        )

    client = GatewayClient(
        config=config,
        state=state,
        opencode_runtime=opencode_runtime,
    )
    try:
        client.run(config.discord_gateway_token)
    finally:
        if opencode_runtime is not None and opencode_runtime.started:
            try:
                asyncio.run(opencode_runtime.stop())
            except RuntimeError:
                # Event loop already closed by discord.py teardown.
                pass
