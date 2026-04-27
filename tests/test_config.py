from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from codex_gateway.config import DEFAULT_RUNTIME_ROOT, discover_ollama_models


class DiscoverOllamaModelsTest(unittest.TestCase):
    def test_default_runtime_root_uses_persistent_codex_home(self) -> None:
        self.assertEqual(
            DEFAULT_RUNTIME_ROOT,
            Path.home() / "codex_gateway_runtime",
        )

    @patch("codex_gateway.config.subprocess.run")
    @patch("codex_gateway.config.shutil.which", return_value=None)
    def test_discovers_models_via_windows_fallback(
        self,
        which_mock,
        run_mock,
    ) -> None:
        del which_mock
        run_mock.return_value.returncode = 0
        run_mock.return_value.stdout = (
            "NAME                         ID              SIZE      MODIFIED\n"
            "llama3.1:latest              abc             4.9 GB    now\n"
            "qwen3:8b                     def             5.2 GB    now\n"
        )

        models = discover_ollama_models()

        self.assertEqual(models, ("llama3.1:latest", "qwen3:8b"))
        run_mock.assert_called_once_with(
            ["cmd.exe", "/c", "ollama", "list"],
            check=False,
            capture_output=True,
            text=True,
        )


class DirectModeConfigDefaultsTest(unittest.TestCase):
    def test_direct_mode_defaults(self) -> None:
        from codex_gateway.config import (
            DEFAULT_OLLAMA_HOST,
            DEFAULT_DIRECT_MAX_ITERATIONS,
            DEFAULT_DIRECT_CONTEXT_CHARS,
            DEFAULT_DIRECT_SHELL_TIMEOUT,
            DEFAULT_DIRECT_TOOL_RESULT_MAX_CHARS,
            DEFAULT_DIRECT_SYSTEM_PROMPT,
        )
        self.assertEqual(DEFAULT_OLLAMA_HOST, "http://localhost:11434")
        self.assertEqual(DEFAULT_DIRECT_MAX_ITERATIONS, 10)
        self.assertEqual(DEFAULT_DIRECT_CONTEXT_CHARS, 90000)
        self.assertEqual(DEFAULT_DIRECT_SHELL_TIMEOUT, 120)
        self.assertEqual(DEFAULT_DIRECT_TOOL_RESULT_MAX_CHARS, 8000)
        self.assertIn("coding assistant", DEFAULT_DIRECT_SYSTEM_PROMPT)
