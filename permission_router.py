from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import discord

from .backend.opencode import OpencodeClient, PermissionAsk

if TYPE_CHECKING:
    from .bot import GatewayClient

LOGGER = logging.getLogger(__name__)


@dataclass
class PendingPermission:
    ask: PermissionAsk
    gateway_session_id: str | None = None
    project_id: str | None = None
    discord_channel_id: int | None = None
    discord_message_id: int | None = None
    timeout_task: asyncio.Task | None = field(default=None, repr=False)


class PermissionRouter:
    """Bridges opencode permission.asked events to Discord operator controls.

    Behaviour matches the agreed UX (C+i+β+R):
    - Posts a button message in the project channel of the affected session.
    - Buttons map to opencode `once|always|reject` responses.
    - On idle timeout, aborts the opencode session (β) and edits the
      message to reflect the expired state.
    - Uses `ask.always[0]` directly when "always" is chosen (R).
    """

    def __init__(
        self,
        gateway_client: "GatewayClient",
        opencode_client: OpencodeClient,
        *,
        timeout_seconds: float = 7200.0,
        allowed_user_ids: set[int] | None = None,
    ) -> None:
        self.gateway = gateway_client
        self.opencode = opencode_client
        self.timeout_seconds = timeout_seconds
        self.allowed_user_ids = allowed_user_ids or set()
        self.pending: dict[str, PendingPermission] = {}
        self._lock = asyncio.Lock()

    async def on_permission_asked(self, ask: PermissionAsk) -> None:
        gateway_session_id, project_id, channel_id = self._lookup(ask.session_id)
        pending = PendingPermission(
            ask=ask,
            gateway_session_id=gateway_session_id,
            project_id=project_id,
            discord_channel_id=channel_id,
        )
        async with self._lock:
            self.pending[ask.permission_id] = pending

        if channel_id is not None:
            try:
                pending.discord_message_id = await self._post_message(
                    channel_id, pending
                )
            except Exception:
                LOGGER.exception(
                    "Failed to post permission ask %s to channel %s",
                    ask.permission_id,
                    channel_id,
                )

        pending.timeout_task = asyncio.create_task(
            self._timeout_watch(ask.permission_id),
            name=f"perm-timeout-{ask.permission_id}",
        )

    async def respond(
        self,
        permission_id: str,
        response: str,
        *,
        responder_label: str | None = None,
    ) -> tuple[bool, str]:
        if response not in {"once", "always", "reject"}:
            return False, f"Invalid response `{response}`."

        async with self._lock:
            pending = self.pending.pop(permission_id, None)

        if pending is None:
            return False, f"Permission `{permission_id}` not found or already resolved."

        if pending.timeout_task is not None and not pending.timeout_task.done():
            pending.timeout_task.cancel()

        try:
            await self.opencode.reply_permission(
                pending.ask.session_id,
                permission_id,
                response,
            )
        except Exception as exc:
            LOGGER.exception("reply_permission failed")
            return False, f"reply_permission failed: {exc}"

        await self._update_message(
            pending,
            outcome=response,
            responder_label=responder_label,
        )
        return True, f"Permission `{permission_id}` → `{response}` recorded."

    def list_pending(self, session_id: str | None = None) -> list[PendingPermission]:
        if session_id is None:
            return list(self.pending.values())
        return [
            pending
            for pending in self.pending.values()
            if pending.ask.session_id == session_id
        ]

    def get_latest_pending_for_session(
        self,
        session_id: str,
    ) -> PendingPermission | None:
        candidates = self.list_pending(session_id)
        if not candidates:
            return None
        candidates.sort(key=lambda p: p.ask.asked_at, reverse=True)
        return candidates[0]

    def _lookup(
        self,
        opencode_session_id: str,
    ) -> tuple[str | None, str | None, int | None]:
        for project in self.gateway.project_registry.load_projects():
            if project.archived:
                continue
            for session in self.gateway.session_store.list_sessions(
                project.project_id
            ):
                if session.codex_thread_ref == opencode_session_id:
                    return (
                        session.session_id,
                        project.project_id,
                        project.project_channel_id,
                    )
        return None, None, None

    async def _post_message(
        self,
        channel_id: int,
        pending: PendingPermission,
    ) -> int | None:
        channel = self.gateway.get_channel(channel_id)
        if channel is None:
            channel = await self.gateway.fetch_channel(channel_id)
        if channel is None:
            return None

        ping = (
            " ".join(f"<@{uid}>" for uid in self.allowed_user_ids)
            if self.allowed_user_ids
            else ""
        )
        embed = self._build_embed(pending)
        view = PermissionView(self, pending.ask.permission_id)
        message = await channel.send(content=ping, embed=embed, view=view)
        return message.id if message is not None else None

    def _build_embed(self, pending: PendingPermission) -> discord.Embed:
        ask = pending.ask
        scope_lines: list[str] = []
        if pending.project_id and pending.gateway_session_id:
            scope_lines.append(
                f"`{pending.project_id}/{pending.gateway_session_id}`"
            )
        scope_lines.append(f"opencode session `{ask.session_id}`")

        embed = discord.Embed(
            title=f"⏸️ Permission asked — {ask.permission}",
            description="\n".join(scope_lines),
            color=0xFFA500,
        )
        if ask.patterns:
            embed.add_field(
                name="Requested",
                value="\n".join(f"`{p}`" for p in ask.patterns)[:1000],
                inline=False,
            )
        if ask.always:
            embed.add_field(
                name="Suggested broad pattern",
                value="\n".join(f"`{p}`" for p in ask.always)[:1000],
                inline=False,
            )
        embed.set_footer(text=f"id: {ask.permission_id}")
        return embed

    async def _update_message(
        self,
        pending: PendingPermission,
        *,
        outcome: str,
        responder_label: str | None = None,
    ) -> None:
        if pending.discord_channel_id is None or pending.discord_message_id is None:
            return
        try:
            channel = self.gateway.get_channel(pending.discord_channel_id)
            if channel is None:
                channel = await self.gateway.fetch_channel(
                    pending.discord_channel_id
                )
            if channel is None:
                return
            message = await channel.fetch_message(pending.discord_message_id)
        except Exception:
            LOGGER.exception("Failed to fetch permission message for update")
            return

        title_prefix = {
            "once": "✅ Approved (once)",
            "always": "♾️ Approved (always)",
            "reject": "❌ Rejected",
            "timeout": "⏰ Operator timeout — session aborted",
        }.get(outcome, f"Resolved: {outcome}")

        embed = message.embeds[0] if message.embeds else discord.Embed()
        embed.title = (
            f"{title_prefix} — {pending.ask.permission}"
            if pending.ask.permission
            else title_prefix
        )
        if responder_label:
            embed.set_footer(
                text=f"id: {pending.ask.permission_id} · resolved by {responder_label}"
            )
        try:
            await message.edit(embed=embed, view=None)
        except Exception:
            LOGGER.exception("Failed to edit permission message")

    async def _timeout_watch(self, permission_id: str) -> None:
        try:
            await asyncio.sleep(self.timeout_seconds)
        except asyncio.CancelledError:
            return

        async with self._lock:
            pending = self.pending.pop(permission_id, None)
        if pending is None:
            return

        try:
            await self.opencode.abort_session(pending.ask.session_id)
        except Exception:
            LOGGER.exception(
                "abort_session failed during timeout for %s",
                pending.ask.session_id,
            )

        await self._update_message(pending, outcome="timeout")


class PermissionView(discord.ui.View):
    """Three-button view attached to the permission ask message."""

    def __init__(self, router: PermissionRouter, permission_id: str) -> None:
        # Discord rejects timeout > 900s for views; cap accordingly. The
        # router still enforces the operational timeout via _timeout_watch,
        # which can cover the longer idle window.
        super().__init__(timeout=min(router.timeout_seconds, 900.0))
        self.router = router
        self.permission_id = permission_id

    async def _is_authorized(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id not in self.router.allowed_user_ids:
            await interaction.response.send_message(
                "You are not authorized to approve permissions.",
                ephemeral=True,
            )
            return False
        return True

    async def _send_outcome(
        self,
        interaction: discord.Interaction,
        response: str,
    ) -> None:
        if not await self._is_authorized(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        _, message = await self.router.respond(
            self.permission_id,
            response,
            responder_label=str(interaction.user),
        )
        await interaction.followup.send(message, ephemeral=True)

    @discord.ui.button(
        label="Once",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="perm:once",
    )
    async def approve_once(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self._send_outcome(interaction, "once")

    @discord.ui.button(
        label="Always",
        style=discord.ButtonStyle.primary,
        emoji="♾️",
        custom_id="perm:always",
    )
    async def approve_always(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self._send_outcome(interaction, "always")

    @discord.ui.button(
        label="Reject",
        style=discord.ButtonStyle.danger,
        emoji="❌",
        custom_id="perm:reject",
    )
    async def reject(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self._send_outcome(interaction, "reject")
