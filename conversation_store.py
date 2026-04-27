from __future__ import annotations

import json
from pathlib import Path


class ConversationStore:
    def __init__(self, state_root: Path) -> None:
        self.state_root = state_root

    def load(self, project_id: str, session_id: str) -> list[dict]:
        path = self._jsonl_path(project_id, session_id)
        if not path.exists():
            return []
        messages: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                messages.append(json.loads(stripped))
            except json.JSONDecodeError:
                continue
        return messages

    def append(
        self,
        project_id: str,
        session_id: str,
        messages: list[dict],
    ) -> None:
        path = self._jsonl_path(project_id, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for message in messages:
                handle.write(
                    json.dumps(message, ensure_ascii=False) + "\n"
                )

    def truncate_to_budget(
        self,
        messages: list[dict],
        max_chars: int,
    ) -> list[dict]:
        if not messages:
            return []

        def _char_size(msg: dict) -> int:
            size = len(json.dumps(msg, ensure_ascii=False))
            return size

        total = sum(_char_size(m) for m in messages)
        if total <= max_chars:
            return list(messages)

        # Always keep the system prompt (index 0 if role == system)
        system_msgs: list[dict] = []
        other_msgs: list[dict] = []
        for msg in messages:
            if msg.get("role") == "system" and not system_msgs:
                system_msgs.append(msg)
            else:
                other_msgs.append(msg)

        system_size = sum(_char_size(m) for m in system_msgs)
        budget = max_chars - system_size

        # Remove oldest non-system messages until within budget
        while other_msgs and sum(_char_size(m) for m in other_msgs) > budget:
            other_msgs.pop(0)

        return system_msgs + other_msgs

    def _jsonl_path(self, project_id: str, session_id: str) -> Path:
        return (
            self.state_root
            / "projects"
            / project_id
            / "sessions"
            / session_id
            / "conversation.jsonl"
        )
