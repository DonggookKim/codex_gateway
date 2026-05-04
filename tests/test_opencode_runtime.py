from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from codex_gateway.backend.opencode_runtime import (
    OpencodeRuntimeSettings,
    opencode_runtime_settings_from_env,
)


class SettingsFromEnvTest(unittest.TestCase):
    def test_returns_none_when_disabled(self) -> None:
        with patch.dict(os.environ, {"OPENCODE_GATEWAY_ENABLED": ""}, clear=True):
            self.assertIsNone(opencode_runtime_settings_from_env())

    def test_returns_none_when_provider_missing(self) -> None:
        with patch.dict(
            os.environ,
            {"OPENCODE_GATEWAY_ENABLED": "1"},
            clear=True,
        ):
            self.assertIsNone(opencode_runtime_settings_from_env())

    def test_builds_settings_with_required_vars(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENCODE_GATEWAY_ENABLED": "true",
                "OPENCODE_PROVIDER_ID": "model-connect",
                "OPENCODE_MODEL_ID": "Qwen3.5",
                "OPENCODE_SERVER_PORT": "14096",
                "OPENCODE_SERVER_HOSTNAME": "127.0.0.1",
                "OPENCODE_SERVER_PASSWORD": "secret",
                "OPENCODE_DEFAULT_AGENT": "build",
                "OPENCODE_IDLE_TIMEOUT_SECONDS": "1800",
            },
            clear=True,
        ):
            settings = opencode_runtime_settings_from_env()

        assert settings is not None
        self.assertIsInstance(settings, OpencodeRuntimeSettings)
        self.assertEqual(settings.provider_id, "model-connect")
        self.assertEqual(settings.model_id, "Qwen3.5")
        self.assertEqual(settings.server.port, 14096)
        self.assertEqual(settings.server.hostname, "127.0.0.1")
        self.assertEqual(settings.server.password, "secret")
        self.assertEqual(settings.default_agent, "build")
        self.assertEqual(settings.idle_timeout_seconds, 1800.0)

    def test_invalid_port_falls_back_to_default(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENCODE_GATEWAY_ENABLED": "1",
                "OPENCODE_PROVIDER_ID": "p",
                "OPENCODE_MODEL_ID": "m",
                "OPENCODE_SERVER_PORT": "not-a-number",
            },
            clear=True,
        ):
            settings = opencode_runtime_settings_from_env()
        assert settings is not None
        self.assertEqual(settings.server.port, 14096)


if __name__ == "__main__":
    unittest.main()
