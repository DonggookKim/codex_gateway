from __future__ import annotations

from pathlib import Path

from .session_inspector import LatestCodexResponse


def write_last_response_text(target: Path, text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    # Use a UTF-8 BOM so Discord-downloaded text opens cleanly in mobile viewers.
    target.write_text(normalized, encoding="utf-8-sig")
    return True


def refresh_last_response_file(
    target: Path,
    latest_response: LatestCodexResponse | None,
) -> bool:
    if latest_response is None:
        return False
    return write_last_response_text(target, latest_response.text)
