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

    def test_returns_empty_list_for_empty_input(self) -> None:
        store = ConversationStore(Path("/unused"))
        self.assertEqual(store.truncate_to_budget([], max_chars=1000), [])

    def test_keeps_only_system_when_budget_smaller_than_history(self) -> None:
        store = ConversationStore(Path("/unused"))
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "x" * 500},
            {"role": "assistant", "content": "y" * 500},
        ]
        result = store.truncate_to_budget(messages, max_chars=100)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["role"], "system")

    def test_no_system_prompt_truncates_oldest(self) -> None:
        store = ConversationStore(Path("/unused"))
        messages = [
            {"role": "user", "content": "a" * 500},
            {"role": "assistant", "content": "b" * 500},
            {"role": "user", "content": "c" * 500},
        ]
        result = store.truncate_to_budget(messages, max_chars=1100)
        # Oldest gone, newest kept
        self.assertTrue(all(m["content"] != "a" * 500 for m in result))
        self.assertEqual(result[-1]["content"], "c" * 500)


class ConversationStoreCorruptionTest(unittest.TestCase):
    def test_load_skips_malformed_jsonl_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationStore(Path(tmp))
            jsonl_path = (
                Path(tmp) / "projects" / "p" / "sessions" / "s"
                / "conversation.jsonl"
            )
            jsonl_path.parent.mkdir(parents=True)
            jsonl_path.write_text(
                json.dumps({"role": "user", "content": "good"}) + "\n"
                + "{not valid json\n"
                + "\n"
                + json.dumps({"role": "assistant", "content": "still good"}) + "\n",
                encoding="utf-8",
            )
            messages = store.load("p", "s")
            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[0]["content"], "good")
            self.assertEqual(messages[1]["content"], "still good")

    def test_append_preserves_unicode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationStore(Path(tmp))
            store.append("p", "s", [{"role": "user", "content": "안녕하세요 🚀"}])
            messages = store.load("p", "s")
            self.assertEqual(messages[0]["content"], "안녕하세요 🚀")
