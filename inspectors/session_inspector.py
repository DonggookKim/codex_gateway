from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class LatestCodexResponse:
    session_id: str
    timestamp: str
    text: str


def _extract_assistant_text(payload: dict) -> str:
    if payload.get("type") != "message" or payload.get("role") != "assistant":
        return ""
    parts: list[str] = []
    for content in payload.get("content", []):
        if content.get("type") == "output_text":
            text = content.get("text", "")
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _extract_event_text(payload: dict) -> str:
    if payload.get("type") == "agent_message":
        return str(payload.get("message", "")).strip()
    return ""


def _iter_session_files(session_root: Path) -> list[Path]:
    if not session_root.exists():
        return []
    return sorted(
        session_root.rglob("*.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def inspect_latest_codex_response(codex_home: Path) -> LatestCodexResponse | None:
    session_root = codex_home / "sessions"

    best: LatestCodexResponse | None = None
    for session_file in _iter_session_files(session_root):
        try:
            with session_file.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    timestamp = str(item.get("timestamp", ""))
                    if not timestamp:
                        continue

                    response_text = ""
                    payload = item.get("payload", {})
                    if item.get("type") == "response_item":
                        response_text = _extract_assistant_text(payload)
                    elif item.get("type") == "event_msg":
                        response_text = _extract_event_text(payload)

                    if not response_text:
                        continue

                    session_id = ""
                    if isinstance(payload, dict):
                        session_id = str(payload.get("session_id", ""))
                    if not session_id:
                        session_id = session_file.stem[-36:]

                    candidate = LatestCodexResponse(
                        session_id=session_id,
                        timestamp=timestamp,
                        text=response_text,
                    )
                    if best is None or candidate.timestamp > best.timestamp:
                        best = candidate
        except OSError:
            continue

        if best is not None:
            break

    return best
