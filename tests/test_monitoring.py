import json
import tempfile
import unittest
from pathlib import Path

from agent2telegram.monitoring import telegram_status


class MonitoringStatusTests(unittest.TestCase):
    def test_formats_safe_multi_agent_status(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "status.json"
            path.write_text(json.dumps({
                "health": "degraded", "checked_at": "2026-07-12T16:00:00Z",
                "agents": {
                    "agentsmith": {"health": "ok", "codex": "active", "bridge": "active", "auth": "ok", "action": "none"},
                    "warren": {"health": "degraded", "codex": "active", "bridge": "active", "auth": "login_required", "action": "none"},
                },
            }), "utf-8")
            text = telegram_status(path)
            self.assertIn("agentsmith", text)
            self.assertIn("warren", text)
            self.assertIn("login_required", text)

    def test_missing_status_is_clear(self):
        self.assertIn("zatím nemá", telegram_status(Path("/missing/status.json")))


if __name__ == "__main__":
    unittest.main()
