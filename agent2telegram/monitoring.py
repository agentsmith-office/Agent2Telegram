"""Read the non-secret central Agents Monitoring status for Telegram commands."""
from __future__ import annotations

import json
from pathlib import Path

STATUS_PATH = Path("/var/lib/agent-smith-monitor/status.json")


def telegram_status(path: Path = STATUS_PATH) -> str:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError, TypeError):
        return "⚪ Monitoring agentů zatím nemá dostupný stav."
    lines = [
        f"{'🟢' if data.get('health') == 'ok' else '🔴'} *Agent Smith monitoring*",
        f"kontrola: `{data.get('checked_at', 'unknown')}`",
    ]
    for name, item in data.get("agents", {}).items():
        icon = "🟢" if item.get("health") == "ok" else "🔴"
        detail = f"Codex {item.get('codex', '?')}, Telegram {item.get('bridge', '?')}, auth {item.get('auth', '?')}"
        if item.get("action") not in (None, "none"):
            detail += f", akce {item['action']}"
        lines.append(f"{icon} *{name}*: {detail}")
    return "\n".join(lines)
