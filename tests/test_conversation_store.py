from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_gateway.conversation_store import ConversationStore


class ConversationStoreLoadTest(unittest.TestCase):
    def test_load_returns_empty_list_when_no_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationStore(Path(tmp))
            messages = store.load("proj1", "sess1")
            self.assertEqual(messages, [])

    def test_load_returns_messages_from_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationStore(Path(tmp))
            jsonl_path = (
                Path(tmp) / "projects" / "proj1" / "sessions" / "sess1"
                / "conversation.jsonl"
            )
            jsonl_path.parent.mkdir(parents=True)
            lines = [
                json.dumps({"role": "system", "content": "hello"}),
                json.dumps({"role": "user", "content": "hi"}),
            ]
            jsonl_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            messages = store.load("proj1", "sess1")
            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[0]["role"], "system")
            self.assertEqual(messages[1]["content"], "hi")


class ConversationStoreAppendTest(unittest.TestCase):
    def test_append_creates_file_and_writes_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationStore(Path(tmp))
            store.append("proj1", "sess1", [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ])
            messages = store.load("proj1", "sess1")
            self.assertEqual(len(messages), 2)

    def test_append_adds_to_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationStore(Path(tmp))
            store.append("proj1", "sess1", [{"role": "user", "content": "a"}])
            store.append("proj1", "sess1", [{"role": "user", "content": "b"}])
            messages = store.load("proj1", "sess1")
            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[1]["content"], "b")


class TruncateToBudgetTest(unittest.TestCase):
    def test_preserves_system_prompt_and_removes_oldest(self) -> None:
        store = ConversationStore(Path("/unused"))
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "a" * 1000},
            {"role": "assistant", "content": "b" * 1000},
            {"role": "user", "content": "c" * 1000},
            {"role": "assistant", "content": "d" * 1000},
        ]
        # Budget fits system + last 2 messages only
        result = store.truncate_to_budget(messages, max_chars=3500)
        self.assertEqual(result[0]["role"], "system")
        self.assertEqual(result[-1]["content"], "d" * 1000)
        # Oldest non-system messages should be removed
        self.assertTrue(len(result) < len(messages))

    def test_returns_all_if_within_budget(self) -> None:
        store = ConversationStore(Path("/unused"))
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ]
        result = store.truncate_to_budget(messages, max_chars=90000)
        self.assertEqual(result, messages)
