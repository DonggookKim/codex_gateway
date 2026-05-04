from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from codex_gateway.backend.opencode import PermissionAsk
from codex_gateway.permission_router import PendingPermission, PermissionRouter


def _make_ask(
    permission_id: str = "per_1",
    session_id: str = "ses_x",
    permission: str = "bash",
    patterns: list[str] | None = None,
    always: list[str] | None = None,
) -> PermissionAsk:
    return PermissionAsk(
        permission_id=permission_id,
        session_id=session_id,
        permission=permission,
        patterns=patterns or ["date"],
        always=always or ["date *"],
        tool_message_id="m",
        tool_call_id="c",
        metadata={},
    )


def _make_session(session_id: str, codex_thread_ref: str) -> MagicMock:
    record = MagicMock()
    record.session_id = session_id
    record.codex_thread_ref = codex_thread_ref
    record.archived = False
    return record


def _make_project(project_id: str, channel_id: int) -> MagicMock:
    project = MagicMock()
    project.project_id = project_id
    project.archived = False
    project.project_channel_id = channel_id
    return project


def _make_router(
    *,
    sessions: list[tuple[str, str, str, int]] | None = None,
    timeout_seconds: float = 5.0,
    allowed: set[int] | None = None,
) -> tuple[PermissionRouter, MagicMock, MagicMock]:
    """sessions: list of (project_id, gateway_session_id, opencode_session_id, channel_id)."""
    sessions = sessions or []
    project_lookup: dict[str, MagicMock] = {}
    session_lookup: dict[str, list[MagicMock]] = {}
    for project_id, gw_id, opencode_id, channel_id in sessions:
        project_lookup.setdefault(
            project_id, _make_project(project_id, channel_id)
        )
        session_lookup.setdefault(project_id, []).append(
            _make_session(gw_id, opencode_id)
        )

    project_registry = MagicMock()
    project_registry.load_projects.return_value = list(project_lookup.values())

    session_store = MagicMock()
    session_store.list_sessions = lambda pid: session_lookup.get(pid, [])

    gateway = MagicMock()
    gateway.project_registry = project_registry
    gateway.session_store = session_store
    gateway.get_channel = MagicMock(return_value=None)
    gateway.fetch_channel = AsyncMock(return_value=None)

    opencode_client = MagicMock()
    opencode_client.reply_permission = AsyncMock(return_value=True)
    opencode_client.abort_session = AsyncMock(return_value=True)

    router = PermissionRouter(
        gateway_client=gateway,
        opencode_client=opencode_client,
        timeout_seconds=timeout_seconds,
        allowed_user_ids=allowed or {1},
    )
    return router, gateway, opencode_client


class LookupTest(unittest.IsolatedAsyncioTestCase):
    async def test_lookup_finds_session_via_codex_thread_ref(self) -> None:
        router, _, _ = _make_router(
            sessions=[("proj", "gw1", "ses_x", 99)],
        )
        gw_id, project_id, channel_id = router._lookup("ses_x")
        self.assertEqual(gw_id, "gw1")
        self.assertEqual(project_id, "proj")
        self.assertEqual(channel_id, 99)

    async def test_lookup_unknown_returns_nones(self) -> None:
        router, _, _ = _make_router()
        self.assertEqual(router._lookup("unknown"), (None, None, None))


class OnPermissionAskedTest(unittest.IsolatedAsyncioTestCase):
    async def test_records_pending_even_when_channel_unknown(self) -> None:
        router, _, _ = _make_router()
        ask = _make_ask()
        # patch _post_message so we don't reach Discord
        router._post_message = AsyncMock(return_value=None)  # type: ignore[assignment]

        await router.on_permission_asked(ask)

        self.assertIn(ask.permission_id, router.pending)
        # channel was None so post_message should not have been called
        router._post_message.assert_not_called()

        # cancel timeout
        pending = router.pending[ask.permission_id]
        if pending.timeout_task is not None:
            pending.timeout_task.cancel()

    async def test_posts_message_when_channel_known(self) -> None:
        router, _, _ = _make_router(
            sessions=[("proj", "gw1", "ses_x", 99)],
        )
        router._post_message = AsyncMock(return_value=12345)  # type: ignore[assignment]
        ask = _make_ask()

        await router.on_permission_asked(ask)

        router._post_message.assert_awaited_once()
        pending = router.pending[ask.permission_id]
        self.assertEqual(pending.discord_channel_id, 99)
        self.assertEqual(pending.discord_message_id, 12345)
        self.assertEqual(pending.gateway_session_id, "gw1")
        self.assertEqual(pending.project_id, "proj")
        if pending.timeout_task is not None:
            pending.timeout_task.cancel()


class RespondTest(unittest.IsolatedAsyncioTestCase):
    async def test_respond_calls_reply_permission_and_clears_pending(self) -> None:
        router, _, opencode = _make_router()
        ask = _make_ask()
        router._post_message = AsyncMock(return_value=None)  # type: ignore[assignment]
        router._update_message = AsyncMock(return_value=None)  # type: ignore[assignment]

        await router.on_permission_asked(ask)
        ok, message = await router.respond(
            ask.permission_id,
            "once",
            responder_label="alice",
        )

        self.assertTrue(ok)
        self.assertIn("recorded", message)
        opencode.reply_permission.assert_awaited_once_with(
            ask.session_id, ask.permission_id, "once"
        )
        self.assertNotIn(ask.permission_id, router.pending)

    async def test_respond_unknown_id_returns_false(self) -> None:
        router, _, _ = _make_router()
        ok, message = await router.respond("per_unknown", "once")
        self.assertFalse(ok)
        self.assertIn("not found", message)

    async def test_respond_invalid_response_rejected(self) -> None:
        router, _, _ = _make_router()
        ok, message = await router.respond("per_1", "approve")
        self.assertFalse(ok)
        self.assertIn("Invalid response", message)


class TimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_aborts_session_and_clears_pending(self) -> None:
        router, _, opencode = _make_router(timeout_seconds=0.05)
        ask = _make_ask()
        router._post_message = AsyncMock(return_value=None)  # type: ignore[assignment]
        router._update_message = AsyncMock(return_value=None)  # type: ignore[assignment]

        await router.on_permission_asked(ask)

        # Wait for timeout task to fire
        await asyncio.sleep(0.15)

        opencode.abort_session.assert_awaited_once_with(ask.session_id)
        self.assertNotIn(ask.permission_id, router.pending)


class LatestPendingTest(unittest.IsolatedAsyncioTestCase):
    async def test_latest_pending_for_session(self) -> None:
        router, _, _ = _make_router()
        router._post_message = AsyncMock(return_value=None)  # type: ignore[assignment]

        ask1 = _make_ask("per_a", session_id="ses_x")
        ask2 = _make_ask("per_b", session_id="ses_x")
        ask2.asked_at = ask1.asked_at + 10

        await router.on_permission_asked(ask1)
        await router.on_permission_asked(ask2)

        latest = router.get_latest_pending_for_session("ses_x")
        assert latest is not None
        self.assertEqual(latest.ask.permission_id, "per_b")

        for pending in list(router.pending.values()):
            if pending.timeout_task is not None:
                pending.timeout_task.cancel()


if __name__ == "__main__":
    unittest.main()
