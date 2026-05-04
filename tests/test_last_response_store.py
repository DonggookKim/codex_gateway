from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_gateway.storage.last_response_store import refresh_last_response_file
from codex_gateway.inspectors.session_inspector import LatestCodexResponse


class LastResponseStoreTest(unittest.TestCase):
    def test_refresh_last_response_file_writes_latest_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "last_response.txt"
            latest = LatestCodexResponse(
                session_id="session-1",
                timestamp="2026-04-21T00:00:00Z",
                text="full assistant response body",
            )

            updated = refresh_last_response_file(target, latest)

            self.assertTrue(updated)
            self.assertTrue(
                target.read_bytes().startswith(b"\xef\xbb\xbf")
            )
            self.assertEqual(
                target.read_text(encoding="utf-8-sig"),
                "full assistant response body",
            )


if __name__ == "__main__":
    unittest.main()
