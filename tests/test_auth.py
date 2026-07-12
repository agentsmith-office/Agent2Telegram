import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent2telegram.auth import AUTH_REQUIRED_NOTICE, AuthState, classify_auth_error
from agent2telegram.attach import _empty_turn_notice
from agent2telegram.stream import StreamBridge


class AuthClassificationTests(unittest.TestCase):
    def test_refresh_token_revoked(self):
        self.assertEqual(
            classify_auth_error("ERROR refresh token was revoked; please log in again"),
            "refresh_token_revoked",
        )

    def test_login_required(self):
        self.assertEqual(classify_auth_error("Login required. Run codex login."), "login_required")

    def test_generic_tool_401_is_not_cli_auth_failure(self):
        self.assertIsNone(classify_auth_error("curl: HTTP 401 Unauthorized from example API"))

    def test_attach_empty_turn_reports_auth_action(self):
        reason, notice = _empty_turn_notice("Refresh token invalid or revoked. Login required.")
        self.assertEqual(reason, "refresh_token_revoked")
        self.assertIn("codex login", notice)


class AuthStateTests(unittest.TestCase):
    def test_failure_is_persistent_deduplicated_and_sanitized(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "auth.json"
            state = AuthState("codex", path=path)
            raw = "refresh token revoked secret-value-which-must-not-be-saved"
            self.assertTrue(state.failure(raw))
            self.assertFalse(state.failure(raw))
            saved = path.read_text("utf-8")
            self.assertNotIn("secret-value", saved)
            self.assertEqual(json.loads(saved)["reason"], "refresh_token_revoked")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_success_recovers_state(self):
        with tempfile.TemporaryDirectory() as td:
            state = AuthState("codex", path=Path(td) / "auth.json")
            state.failure("login required")
            state.success()
            self.assertEqual(state.status, "ok")
            self.assertEqual(state.data["reason"], "")

    def test_probe_does_not_treat_stored_credentials_as_verified(self):
        with tempfile.TemporaryDirectory() as td, \
             patch("agent2telegram.auth.shutil.which", return_value="/usr/bin/codex"), \
             patch("agent2telegram.auth.subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="Logged in", stderr="")
            state = AuthState("codex", path=Path(td) / "auth.json")
            self.assertEqual(state.probe(), "unknown")
            self.assertEqual(run.call_args.args[0], ["/usr/bin/codex", "login", "status"])

    def test_probe_does_not_clear_a_real_auth_failure(self):
        with tempfile.TemporaryDirectory() as td, \
             patch("agent2telegram.auth.shutil.which", return_value="/usr/bin/codex"), \
             patch("agent2telegram.auth.subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="Logged in", stderr="")
            state = AuthState("codex", path=Path(td) / "auth.json")
            state.failure("refresh token was revoked")
            self.assertEqual(state.probe(), "login_required")


class StreamAuthTests(unittest.TestCase):
    def test_stream_notifies_once_per_failed_turn_without_leaking_error(self):
        with tempfile.TemporaryDirectory() as td:
            sent = []
            bridge = object.__new__(StreamBridge)
            bridge._auth = AuthState("codex", path=Path(td) / "auth.json")
            bridge._owner_chat = 7
            bridge._turn_text_sent = False
            bridge.tg = SimpleNamespace(send_message=lambda chat, text: sent.append((chat, text)))
            bridge._notify_auth("refresh token revoked TOP-SECRET")
            bridge._notify_auth("refresh token revoked TOP-SECRET")
            self.assertEqual(sent, [(7, AUTH_REQUIRED_NOTICE)])
            self.assertNotIn("TOP-SECRET", sent[0][1])


if __name__ == "__main__":
    unittest.main()
