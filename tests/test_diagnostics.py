import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent2telegram.attach import AttachBridge
from agent2telegram.auth import AuthState


class _Telegram:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, **_kwargs):
        self.sent.append((chat_id, text))


class DiagnosticCommandTests(unittest.TestCase):
    def setUp(self):
        self.bridge = object.__new__(AttachBridge)
        self.bridge.cfg = SimpleNamespace(
            agent="codex", tmux_session="moneypenny", elevenlabs_api_key=""
        )
        self.bridge.tg = _Telegram()
        self.bridge._session = SimpleNamespace(alive=True)
        self.bridge._turn_active = threading.Event()
        self.bridge._pending_send = []
        self.bridge._transcript = Path("rollout-safe.jsonl")
        self.bridge._bridge_started = time.monotonic() - 10
        self.bridge._stall_level = 0
        self.bridge._turn_started_wall = 0
        self.bridge._last_activity_wall = 0
        self.bridge._runtime_path = None
        self.bridge._cancel_requested = False
        self.bridge._cancel_in_progress = False
        self.bridge._cancel_lock = threading.Lock()
        self.bridge._auth = AuthState("codex", path=Path("/nonexistent/test-auth-state.json"))

    def test_health_reports_runtime_state(self):
        self.assertTrue(self.bridge._handle_command("/health", 7))
        text = self.bridge.tg.sent[-1][1]
        self.assertIn("Bridge: running", text)
        self.assertIn("moneypenny", text)
        self.assertIn("outbound queue: 0", text)

    def test_diag_contains_no_config_or_token(self):
        self.assertTrue(self.bridge._handle_command("/diag", 7))
        text = self.bridge.tg.sent[-1][1]
        self.assertIn("Agent2Telegram diagnostics", text)
        self.assertIn("rollout-safe.jsonl", text)
        self.assertNotIn("token", text.lower())

    def test_cancel_interrupts_only_an_active_turn(self):
        interrupted = []
        self.bridge._session = SimpleNamespace(
            alive=True, interrupt=lambda: interrupted.append(True)
        )
        self.bridge._turn_active.set()

        with patch("agent2telegram.attach.threading.Thread") as thread:
            self.assertTrue(self.bridge._handle_command("/cancel", 7))

        self.assertEqual(interrupted, [])
        thread.return_value.start.assert_called_once_with()
        self.assertTrue(self.bridge._turn_active.is_set())
        self.assertIn("ověřuji", self.bridge.tg.sent[-1][1])

    def test_cancel_is_a_noop_when_idle(self):
        self.bridge._session = SimpleNamespace(
            alive=True, interrupt=lambda: self.fail("idle session was interrupted")
        )

        self.assertTrue(self.bridge._handle_command("/cancel", 7))

        self.assertIn("Žádný aktivní úkol", self.bridge.tg.sent[-1][1])


if __name__ == "__main__":
    unittest.main()
