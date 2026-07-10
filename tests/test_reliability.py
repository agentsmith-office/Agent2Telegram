import threading
import time
import unittest

from agent2telegram.attach import AttachBridge


class TurnBackstopTests(unittest.TestCase):
    def test_finish_turn_forwards_unsent_final_answer(self):
        bridge = object.__new__(AttachBridge)
        bridge._turn_active = threading.Event()
        bridge._turn_active.set()
        bridge._turn_from_tg = True
        bridge._turn_text_sent = False
        bridge._owner_chat = 42
        bridge._pending_turn_end = False
        bridge._turn_end = None
        bridge._turn_started = time.monotonic()
        bridge._typing_count = 1
        bridge._max_gap = 0.0
        bridge._status_clear = lambda: None
        bridge._last_assistant_text = lambda: "[TG] RECOVERED"
        bridge._strip_marker = lambda text: text.removeprefix("[TG] ")
        sent = []
        bridge._send_final = lambda text, key=None: sent.append(text)

        bridge._finish_turn()

        self.assertEqual(sent, ["RECOVERED"])
        self.assertFalse(bridge._turn_active.is_set())

    def test_codex_reader_requires_authoritative_turn_end(self):
        from agent2telegram.readers import CodexReader, ClaudeCodeReader

        self.assertTrue(CodexReader.emits_turn_end)
        self.assertFalse(ClaudeCodeReader.emits_turn_end)


if __name__ == "__main__":
    unittest.main()
