"""Safe Codex CLI authentication failure detection and persistent health state."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .config import _state_dir

AUTH_REQUIRED_NOTICE = (
    "⚠️ Codex CLI není přihlášen nebo byl jeho refresh token zneplatněn. "
    "Agent2Telegram běží, ale Codex nemůže odpovídat. Přihlaste se na server pod stejným "
    "systémovým účtem, spusťte `codex login` a potom požadavek zopakujte."
)

# Match only explicit authentication language. Generic HTTP 401/403 can come from tools called
# by Codex and must not incorrectly mark the CLI login as broken.
_AUTH_PATTERNS = (
    ("refresh_token_revoked", re.compile(
        r"refresh\s+token.{0,80}(?:revoked|invalid|expired)|"
        r"(?:revoked|invalid|expired).{0,80}refresh\s+token", re.I | re.S)),
    ("login_required", re.compile(
        r"\blog(?:in|\s+in)\s+required\b|\bnot\s+(?:logged|signed)\s+in\b|"
        r"\bplease\s+(?:log|sign)\s+in\b|\b(?:run|use)\s+[`']?codex\s+login\b|"
        r"(?:codex|chatgpt|openai).{0,100}(?:login required|not\s+(?:logged|signed)\s+in)",
        re.I | re.S)),
    ("authentication_failed", re.compile(
        r"(?:codex|chatgpt|openai).{0,100}(?:authentication|authorization).{0,40}"
        r"(?:failed|invalid|revoked|required)|"
        r"(?:authentication|authorization).{0,40}(?:failed|invalid|revoked|required).{0,100}"
        r"(?:codex|chatgpt|openai)", re.I | re.S)),
)


def classify_auth_error(text: str) -> str | None:
    """Return a non-secret reason code for an explicit Codex authentication failure."""
    sample = (text or "")[-8000:]
    for reason, pattern in _AUTH_PATTERNS:
        if pattern.search(sample):
            return reason
    return None


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class AuthState:
    """Persist only classified state; never raw CLI output or credentials."""

    def __init__(self, agent: str, *, path: Path | None = None) -> None:
        self.agent = agent
        self.path = path or (_state_dir() / f"{agent}_auth.json")
        self.data = self._load()

    def _load(self) -> dict:
        if self.agent != "codex":
            return {"status": "not_applicable", "reason": "", "checked_at": ""}
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            if raw.get("status") in {"unknown", "ok", "login_required"}:
                return {k: raw.get(k, "") for k in
                        ("status", "reason", "checked_at", "notified_at")}
        except (OSError, ValueError, TypeError):
            pass
        return {"status": "unknown", "reason": "", "checked_at": "", "notified_at": ""}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, stat.S_IRWXU)
        except OSError:
            pass
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), "utf-8")
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, self.path)

    def failure(self, text: str) -> bool:
        """Record an auth failure. Return True only once per transition for notification."""
        reason = classify_auth_error(text)
        if not reason or self.agent != "codex":
            return False
        notify = self.data.get("status") != "login_required"
        self.data.update(status="login_required", reason=reason, checked_at=_now())
        if notify:
            self.data["notified_at"] = self.data["checked_at"]
        self._save()
        return notify

    def success(self) -> None:
        if self.agent != "codex" or self.data.get("status") == "ok":
            return
        self.data.update(status="ok", reason="", checked_at=_now(), notified_at="")
        self._save()

    def probe(self) -> str:
        """Detect an explicit missing login without claiming that stored credentials still work.

        ``codex login status`` only proves that credentials are present.  It does not exercise a
        refresh token, so a zero exit status must not promote an unknown or failed state to OK.
        A real successful Codex response is the authoritative recovery signal.
        """
        if self.agent != "codex":
            return self.status
        binary = shutil.which("codex")
        if not binary:
            return self.status
        try:
            proc = subprocess.run(
                [binary, "login", "status"], capture_output=True, text=True,
                stdin=subprocess.DEVNULL, timeout=5, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return self.status
        output = "\n".join((proc.stdout or "", proc.stderr or ""))
        if proc.returncode != 0 and classify_auth_error(output):
            self.failure(output)
        return self.status

    @property
    def status(self) -> str:
        return str(self.data.get("status", "unknown"))

    def health_line(self) -> str:
        labels = {
            "ok": "ok",
            "login_required": "LOGIN REQUIRED",
            "unknown": "unknown (not checked yet)",
            "not_applicable": "not applicable",
        }
        return f"Codex auth: {labels.get(self.status, self.status)}"

    def diag_lines(self) -> list[str]:
        lines = [f"auth status: `{self.status}`"]
        if self.data.get("reason"):
            lines.append(f"auth reason: `{self.data['reason']}`")
        if self.data.get("checked_at"):
            lines.append(f"auth checked: `{self.data['checked_at']}`")
        return lines
