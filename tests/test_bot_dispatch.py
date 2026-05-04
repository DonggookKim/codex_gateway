from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from codex_gateway.bot import GatewayClient
from codex_gateway.config import GatewayConfig
from codex_gateway.session_store import SessionStore
from codex_gateway.state import GatewayState, LastRunSummary, utc_now


def _make_summary() -> LastRunSummary:
    return LastRunSummary(
        run_id="r1",
        requester_user_id=1,
        requester_name="op",
        prompt_excerpt="hi",
        started_at=utc_now(),
        finished_at=utc_now(),
        exit_code=0,
        exit_signal=None,
        stdout_excerpt="",
        stderr_excerpt="",
        assistant_response_excerpt="ok",
        codex_thread_ref="ses_x",
    )


def _build_client(
    root: Path,
    *,
    opencode_runtime=None,
) -> GatewayClient:
    config = GatewayConfig(
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
    state = GatewayState(config.state_file)
    return GatewayClient(
        config=config,
        state=state,
        opencode_runtime=opencode_runtime,
    )


class RunAttemptDispatchTest(unittest.IsolatedAsyncioTestCase):
    async def test_codex_session_uses_run_codex(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            client = _build_client(root)
            store = SessionStore(client.config.state_root, client.config.runtime_root)
            session = store.create_session(
                project_id="proj",
                label="codex-session",
                model_profile="gpt-5.4",
                backend="codex",
            )

            summary = _make_summary()
            with patch(
                "codex_gateway.bot.run_codex",
                new=AsyncMock(return_value=summary),
            ) as run_codex_mock:
                result = await client._run_codex_attempt(
                    requester_id=1,
                    requester_name="op",
                    prompt="hello",
                    project_id="proj",
                    session_id=session.session_id,
                    codex_session_ref="thread-1",
                    discord_channel_name="ch",
                    model_profile="gpt-5.4",
                    project_label="proj",
                    session_label=session.label,
                )

            self.assertIs(result, summary)
            run_codex_mock.assert_awaited_once()

    async def test_opencode_session_dispatches_to_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)

            mock_backend = MagicMock()
            mock_backend.run = AsyncMock(return_value=_make_summary())
            mock_runtime = MagicMock()
            mock_runtime.start = AsyncMock(return_value=mock_backend)

            client = _build_client(root, opencode_runtime=mock_runtime)
            store = SessionStore(client.config.state_root, client.config.runtime_root)
            session = store.create_session(
                project_id="proj",
                label="opencode-session",
                model_profile="model-connect/Qwen3.5",
                backend="opencode",
            )

            with patch(
                "codex_gateway.bot.run_codex",
                new=AsyncMock(side_effect=AssertionError("run_codex must not be called")),
            ):
                result = await client._run_codex_attempt(
                    requester_id=42,
                    requester_name="op",
                    prompt="ping",
                    project_id="proj",
                    session_id=session.session_id,
                    codex_session_ref=None,
                    discord_channel_name=None,
                    model_profile="model-connect/Qwen3.5",
                    project_label="proj",
                    session_label=session.label,
                )

            self.assertIsNotNone(result)
            mock_runtime.start.assert_awaited_once()
            mock_backend.run.assert_awaited_once()
            request = mock_backend.run.await_args.args[2]
            self.assertEqual(request.prompt, "ping")
            self.assertEqual(request.model_profile, "model-connect/Qwen3.5")
            self.assertTrue(request.start_new_session)

    async def test_opencode_session_without_runtime_raises(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            client = _build_client(root, opencode_runtime=None)
            store = SessionStore(client.config.state_root, client.config.runtime_root)
            session = store.create_session(
                project_id="proj",
                label="opencode-session",
                model_profile="model-connect/Qwen3.5",
                backend="opencode",
            )

            with self.assertRaises(RuntimeError):
                await client._run_codex_attempt(
                    requester_id=1,
                    requester_name="op",
                    prompt="x",
                    project_id="proj",
                    session_id=session.session_id,
                    codex_session_ref=None,
                    discord_channel_name=None,
                    model_profile="model-connect/Qwen3.5",
                    project_label="proj",
                    session_label=session.label,
                )


if __name__ == "__main__":
    unittest.main()
