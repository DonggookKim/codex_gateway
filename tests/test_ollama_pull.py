from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from codex_gateway.ollama_pull import (
    OllamaPullError,
    _validate_tag,
    ensure_pulled,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class ValidateTagTest(unittest.TestCase):
    def test_accepts_normal_tags(self) -> None:
        self.assertEqual(_validate_tag("qwen3:14b"), "qwen3:14b")
        self.assertEqual(_validate_tag("mistral-nemo:latest"), "mistral-nemo:latest")
        self.assertEqual(_validate_tag("namespace/model:tag"), "namespace/model:tag")

    def test_rejects_shell_metacharacters(self) -> None:
        for bad in (
            "foo;rm -rf /",
            "foo bar",
            "foo$(whoami)",
            "foo`uname`",
            "",
            "   ",
            "foo|bar",
        ):
            with self.assertRaises(OllamaPullError, msg=f"should reject {bad!r}"):
                _validate_tag(bad)


class EnsurePulledTest(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self) -> None:
        self.loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())

    def test_skips_pull_when_already_local(self) -> None:
        sink = AsyncMock()
        with patch(
            "codex_gateway.ollama_pull.list_local_models",
            new=AsyncMock(return_value=["qwen3:14b", "mistral-nemo:latest"]),
        ):
            with patch(
                "codex_gateway.ollama_pull.pull",
            ) as pull_mock:
                ok = self.loop.run_until_complete(
                    ensure_pulled("qwen3:14b", progress_sink=sink)
                )
        self.assertTrue(ok)
        pull_mock.assert_not_called()
        # one informational sink call about skipping
        self.assertEqual(sink.await_count, 1)
        msg = sink.await_args.args[0]
        self.assertIn("already pulled", msg)

    def test_runs_pull_when_missing_and_only_emits_start_and_end(self) -> None:
        sink = AsyncMock()

        async def fake_pull(tag, *, ollama_bin=None, progress_interval_seconds=5.0):
            from codex_gateway.ollama_pull import _PullStatus

            # Multiple intermediate snapshots — these MUST be suppressed so
            # Discord is not spammed with per-percent ticks.
            yield _PullStatus(message="pulling 25%", done=False, success=False)
            yield _PullStatus(message="pulling 75%", done=False, success=False)
            yield _PullStatus(
                message=f"✅ `ollama pull {tag}` complete", done=True, success=True
            )

        with patch(
            "codex_gateway.ollama_pull.list_local_models",
            new=AsyncMock(return_value=["qwen3:14b"]),
        ):
            with patch(
                "codex_gateway.ollama_pull.pull",
                side_effect=fake_pull,
            ):
                ok = self.loop.run_until_complete(
                    ensure_pulled("mistral-nemo:latest", progress_sink=sink)
                )
        self.assertTrue(ok)
        # initial "not found" notice + final success ONLY = 2
        self.assertEqual(sink.await_count, 2)
        messages = [c.args[0] for c in sink.await_args_list]
        self.assertTrue(any("not found locally" in m for m in messages))
        self.assertTrue(any("complete" in m for m in messages))
        # And critically, no intermediate progress noise reaches the sink.
        self.assertFalse(any("pulling 25%" in m for m in messages))
        self.assertFalse(any("pulling 75%" in m for m in messages))

    def test_returns_false_on_pull_failure(self) -> None:
        sink = AsyncMock()

        async def failing_pull(tag, *, ollama_bin=None, progress_interval_seconds=5.0):
            from codex_gateway.ollama_pull import _PullStatus

            yield _PullStatus(
                message=f"❌ `ollama pull {tag}` failed: network error",
                done=True,
                success=False,
            )

        with patch(
            "codex_gateway.ollama_pull.list_local_models",
            new=AsyncMock(return_value=[]),
        ):
            with patch(
                "codex_gateway.ollama_pull.pull",
                side_effect=failing_pull,
            ):
                ok = self.loop.run_until_complete(
                    ensure_pulled("nonexistent:9999", progress_sink=sink)
                )
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
