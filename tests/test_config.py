from __future__ import annotations

from pathlib import Path
import unittest

from codex_gateway.config import DEFAULT_RUNTIME_ROOT


class ConfigDefaultsTest(unittest.TestCase):
    def test_default_runtime_root_uses_persistent_codex_home(self) -> None:
        self.assertEqual(
            DEFAULT_RUNTIME_ROOT,
            Path.home() / "codex_gateway_runtime",
        )


if __name__ == "__main__":
    unittest.main()
