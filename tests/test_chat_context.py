import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent2telegram.chat_context import (
    acknowledge_proactive,
    enrich_inbound,
    pending_proactive,
    record_proactive,
)
from agent2telegram.__main__ import _record_proactive_best_effort


class ProactiveContextTests(unittest.TestCase):
    def test_confirmed_delivery_survives_any_context_recorder_failure(self):
        def fail(*_args):
            raise RuntimeError("sandbox denied context lock")

        _record_proactive_best_effort(fail, 42, "sent", [100])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"AGENT2TELEGRAM_STATE": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_notification_is_visible_until_acknowledged(self):
        record_proactive(42, "Audit asks: reply yes", [100])

        records, cursor = pending_proactive(42)
        self.assertEqual([r["text"] for r in records], ["Audit asks: reply yes"])
        self.assertEqual(cursor, 100)

        acknowledge_proactive(42, cursor)
        self.assertEqual(pending_proactive(42), ([], None))
        journal = Path(self.tmp.name) / "proactive-messages.jsonl"
        self.assertEqual(journal.read_text("utf-8"), "")

    def test_other_chat_never_receives_context(self):
        record_proactive(42, "private notice", [100])
        enriched, cursor = enrich_inbound({"chat": {"id": 99}}, "hello")
        self.assertEqual(enriched, "hello")
        self.assertIsNone(cursor)

    def test_inbound_contains_proactive_and_explicit_reply_context(self):
        record_proactive(42, "Mám opravit vše?", [100])
        msg = {
            "chat": {"id": 42},
            "text": "ano",
            "reply_to_message": {"message_id": 100, "text": "Mám opravit vše?"},
        }
        enriched, cursor = enrich_inbound(msg, "ano")

        self.assertIn("automatické zprávy", enriched)
        self.assertIn("výslovně odpovídá", enriched)
        self.assertTrue(enriched.endswith("ano"))
        self.assertEqual(cursor, 100)

    def test_journal_and_cursor_are_private(self):
        record_proactive(42, "notice", [100])
        acknowledge_proactive(42, 100)
        for name in ("proactive-messages.jsonl", "proactive-context-cursors.json"):
            mode = (Path(self.tmp.name) / name).stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_corrupt_journal_line_is_ignored(self):
        path = Path(self.tmp.name) / "proactive-messages.jsonl"
        path.write_text("not json\n" + json.dumps({
            "chat_id": 42, "message_id": 2, "text": "valid"
        }) + "\n", "utf-8")
        records, cursor = pending_proactive(42)
        self.assertEqual([r["text"] for r in records], ["valid"])
        self.assertEqual(cursor, 2)


if __name__ == "__main__":
    unittest.main()
