"""Durable context for proactive Telegram messages.

Telegram does not send a bot's own ``sendMessage`` calls back through ``getUpdates``.
Without a small local journal, notifications emitted by ``agent2telegram notify`` are
therefore visible to the human but invisible to the attached agent session.
"""
from __future__ import annotations

import json
import os
import stat
import time
import fcntl
from contextlib import contextmanager
from pathlib import Path

MAX_CONTEXT_CHARS = 12_000
MAX_PENDING_MESSAGES = 20
MAX_QUOTED_CHARS = 4_000


def _state_dir() -> Path:
    return Path(
        os.environ.get(
            "AGENT2TELEGRAM_STATE",
            Path.home() / ".local" / "state" / "agent2telegram",
        )
    ).expanduser()


def _journal_path() -> Path:
    return _state_dir() / "proactive-messages.jsonl"


def _cursor_path() -> Path:
    return _state_dir() / "proactive-context-cursors.json"


def _lock_path() -> Path:
    return _state_dir() / "proactive-context.lock"


def _secure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, stat.S_IRWXU)
    except OSError:
        pass


@contextmanager
def _locked(*, exclusive: bool):
    path = _lock_path()
    _secure_parent(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def record_proactive(chat_id: int, text: str, message_ids: list[int]) -> None:
    """Record a notification only after Telegram confirmed its delivery."""
    if not text or not message_ids:
        return
    path = _journal_path()
    _secure_parent(path)
    record = {
        "chat_id": int(chat_id),
        "message_id": max(int(i) for i in message_ids),
        "message_ids": [int(i) for i in message_ids],
        "sent_at": int(time.time()),
        "text": text,
    }
    # Each append is a single write and notification payloads are bounded by Telegram's
    # own message limits. This keeps concurrent cron notifiers from replacing each other.
    with _locked(exclusive=True):
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
        finally:
            os.close(fd)


def _cursors() -> dict[str, int]:
    try:
        raw = json.loads(_cursor_path().read_text("utf-8"))
        return {str(k): int(v) for k, v in raw.items()}
    except (OSError, ValueError, TypeError):
        return {}


def pending_proactive(chat_id: int) -> tuple[list[dict], int | None]:
    """Return unseen notifications for one chat and their acknowledgement cursor."""
    found: list[dict] = []
    with _locked(exclusive=False):
        cursor = _cursors().get(str(chat_id), 0)
        try:
            lines = _journal_path().read_text("utf-8").splitlines()
        except OSError:
            return [], None
    for line in lines:
        try:
            record = json.loads(line)
            if int(record.get("chat_id")) != int(chat_id):
                continue
            if int(record.get("message_id", 0)) <= cursor:
                continue
            if not isinstance(record.get("text"), str):
                continue
            found.append(record)
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
    found = found[-MAX_PENDING_MESSAGES:]
    if not found:
        return [], None
    return found, max(int(r["message_id"]) for r in found)


def acknowledge_proactive(chat_id: int, message_id: int | None) -> None:
    if message_id is None:
        return
    path = _cursor_path()
    _secure_parent(path)
    with _locked(exclusive=True):
        cursors = _cursors()
        key = str(chat_id)
        cursors[key] = max(cursors.get(key, 0), int(message_id))
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cursors, sort_keys=True) + "\n", "utf-8")
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)

        # Minimise retention: acknowledged chat text no longer needs to remain on disk.
        journal = _journal_path()
        try:
            lines = journal.read_text("utf-8").splitlines()
        except OSError:
            return
        keep: list[str] = []
        for line in lines:
            try:
                record = json.loads(line)
                consumed = (
                    int(record.get("chat_id")) == int(chat_id)
                    and int(record.get("message_id", 0)) <= cursors[key]
                )
            except (ValueError, TypeError, json.JSONDecodeError):
                consumed = False
            if not consumed:
                keep.append(line)
        journal_tmp = journal.with_suffix(".tmp")
        journal_tmp.write_text("\n".join(keep) + ("\n" if keep else ""), "utf-8")
        os.chmod(journal_tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(journal_tmp, journal)


def enrich_inbound(msg: dict, text: str) -> tuple[str, int | None]:
    """Add bot messages the agent missed and Telegram's explicit reply target."""
    chat_id = int(msg["chat"]["id"])
    pending, cursor = pending_proactive(chat_id)
    blocks: list[str] = []
    if pending:
        rendered = "\n\n".join(str(r["text"]).strip() for r in pending)
        if len(rendered) > MAX_CONTEXT_CHARS:
            rendered = rendered[-MAX_CONTEXT_CHARS:]
            rendered = "[starší část byla zkrácena]\n" + rendered
        blocks.append(
            "[Kontext Telegramu — automatické zprávy, které tento bot odeslal "
            "od poslední uživatelské zprávy:]\n" + rendered
        )

    replied = msg.get("reply_to_message") or {}
    quoted = (replied.get("text") or replied.get("caption") or "").strip()
    if quoted:
        if len(quoted) > MAX_QUOTED_CHARS:
            quoted = quoted[:MAX_QUOTED_CHARS] + "…"
        blocks.append(
            "[Uživatel v Telegramu výslovně odpovídá na tuto zprávu:]\n" + quoted
        )

    blocks.append(text)
    return "\n\n".join(block for block in blocks if block), cursor
