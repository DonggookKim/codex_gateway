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
