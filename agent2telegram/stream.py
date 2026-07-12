"""Stream mode — drive Codex via ``codex exec --json`` and forward its **live** event stream.

Attach mode tails an agent's transcript file, which Codex only writes at step *completion* (so
tool calls / web searches show up batched, after the fact). Stream mode instead spawns
``codex exec --json`` per message and reads its JSONL event stream from stdout in real time:

  thread.started        → the conversation id (used to resume, so context persists)
  turn.started          → a turn began
  item.started/<tool>   → a tool/web-search/command started  → live status bubble
  item.completed/agent_message → assistant text              → forwarded (kept) message
  turn.completed        → the turn finished                  → bubble cleared, typing off

This gives the same live UX as Claude Code (progress, tool bubbles, typing) for Codex, and
removes the "terminal shows it instantly, Telegram lags" effect. The whole Telegram side
(sending, the italic status bubble, the typing thread, slash commands, voice, the dedup ledger)
is reused from :class:`~agent2telegram.attach.AttachBridge`.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .attach import AttachBridge
from .auth import AUTH_REQUIRED_NOTICE, AuthState, classify_auth_error
from .config import Config
from .telegram import TelegramClient

log = logging.getLogger("agent2telegram.stream")


def _stream_tool_summary(item: dict) -> str:
    """One-line bubble for a Codex ``codex exec --json`` item (tool / web search / command)."""
    from .readers import _short
    it = item.get("type")
    if it == "web_search":
        action = item.get("action") if isinstance(item.get("action"), dict) else {}
        q = item.get("query") or action.get("query") or (action.get("queries") or [""])[0] or ""
        return "🔎 Web search: " + _short(q) if q else "🔎 Searching the web"
    if it == "command_execution":
        return "🛠️ " + _short(item.get("command", "command"))
    if it == "file_change":
        return "✏️ " + _short("editing files")
    if it == "mcp_tool_call":
        return "🔌 " + _short(item.get("tool") or item.get("server") or "mcp tool")
    if it == "todo_list":
        return "🗒️ " + _short("plan updated")
    return "🛠️ " + _short(str(it or "tool"))


class StreamBridge(AttachBridge):
    """Codex via ``codex exec --json``. Reuses AttachBridge's Telegram side; replaces the
    transcript-tailing source with a live subprocess event stream."""

    @staticmethod
    def _codex_bin() -> str:
        """Resolve the codex binary: PATH, then the common ~/.local/bin install (which is on PATH
        only in login shells), else bare 'codex'. Used as an absolute path when spawning, so a
        minimal (non-login) launcher environment still finds it."""
        found = shutil.which("codex")
        if found:
            return found
        local = Path.home() / ".local" / "bin" / "codex"
        return str(local) if local.exists() else "codex"

    def __init__(self, cfg: Config, *, client: TelegramClient | None = None) -> None:
        self._codex = self._codex_bin()
        if self._codex == "codex" and not shutil.which("codex"):
            log.warning("codex binary not found on PATH or ~/.local/bin — will try 'codex' anyway")
        # ---- Telegram-side state (mirrors AttachBridge.__init__, minus tmux/transcript) ----
        self.cfg = cfg
        self.tg = client or TelegramClient(cfg.token)
        self._auth = AuthState(cfg.agent)
        self._allowed = set(cfg.allowed_user_ids)
        self._marker = cfg.progress_marker
        self._origin = cfg.origin_prefix
        self._origins = tuple({p for p in (cfg.origin_prefix.strip(), "Telegram:", "[TG]") if p})
        self._owner_chat = cfg.allowed_user_ids[0] if cfg.allowed_user_ids else None
        self._signal = Path(cfg.signal_file) if cfg.signal_file else None
        self._turn_end = None                      # no Stop-hook marker in stream mode
        self._stop = threading.Event()
        self._sent_path = Path.home() / ".config" / "agent2telegram" / "stream_sent.txt"
        try:
            self._sent_keys: set = set(self._sent_path.read_text("utf-8").split())
        except OSError:
            self._sent_keys = set()
        self._turn_active = threading.Event()
        self._turn_from_tg = True                  # every turn here is Telegram-originated
        self._last_activity = 0.0
        self._status = {"mid": None, "shown": ""}
        self._last_typing = 0.0
        self._typing_count = 0
        self._turn_started = 0.0
        self._max_gap = 0.0
        self._status_path = (self._signal.parent / "stream_status_bubble") if self._signal else None
        self._seen_tools: set = set()
        self._pending_turn_end = False
        self._turn_text_sent = False
        self._bridge_started = time.monotonic()
        # ---- Codex stream specifics ----
        self._thread_id: str | None = None         # conversation id → resume keeps context
        self._proc_lock = threading.Lock()         # one turn at a time

    # ---- lifecycle ---------------------------------------------------------
    def run(self) -> None:
        self._auth.probe()
        me = self.tg.get_me()
        log.info("Stream bridge live as @%s → codex exec --json, owner=%s",
                 me.get("username"), self._owner_chat)
        from .attach import BOT_COMMANDS
        self.tg.set_my_commands(BOT_COMMANDS)
        self._cleanup_orphan_status()
        threading.Thread(target=self._typing_loop, daemon=True).start()
        self._inbound_loop()

    # ---- inbound (override): spawn codex exec --json instead of tmux send-keys ----
    def _inject(self, text: str) -> None:
        """Run one Codex turn as a streamed subprocess; forward its events live."""
        threading.Thread(target=self._run_turn, args=(text,), daemon=True).start()

    def _codex_argv(self, prompt: str) -> list:
        base = ["codex", "exec", "--json"]
        if self.cfg.command:                        # user override (without {prompt})
            base = [a for a in self.cfg.command if a != "{prompt}"]
        if base and base[0] == "codex":             # spawn by absolute path (PATH-independent)
            base[0] = self._codex
        if self._thread_id:                          # ['codex','exec', ...] → insert resume <id>
            base = base[:2] + ["resume", self._thread_id] + base[2:]
        return base + [prompt]

    def _run_turn(self, text: str) -> None:
        with self._proc_lock:                       # serialize turns (one Codex at a time)
            now = time.monotonic()
            self._turn_active.set()
            self._last_activity = self._turn_started = self._last_typing = now
            self._typing_count = 1
            self._max_gap = 0.0
            self._turn_text_sent = False
            if self._owner_chat is not None:
                self.tg.send_chat_action(self._owner_chat, "typing")
            log.info("TURN START t=%.2f", time.time())
            argv = self._codex_argv(text)
            try:
                proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, cwd=str(Path.home()))
            except OSError as e:
                log.error("codex spawn failed: %s", e)
                if self._owner_chat is not None:
                    self.tg.send_message(self._owner_chat, f"⚠️ Couldn't start Codex: {e}")
                    self._turn_text_sent = True
                self._finish_turn()
                return
            stderr_chunks: list[str] = []

            def read_stderr() -> None:
                if proc.stderr is None:
                    return
                for chunk in iter(lambda: proc.stderr.read(2048), ""):
                    stderr_chunks.append(chunk)
                    if sum(map(len, stderr_chunks)) > 16000:
                        del stderr_chunks[:-4]

            stderr_thread = threading.Thread(target=read_stderr, daemon=True)
            stderr_thread.start()
            try:
                for line in proc.stdout:            # blocking read, line by line (live)
                    line = line.strip()
                    if not line:
                        continue
                    self._last_activity = time.monotonic()
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if classify_auth_error(line):
                        self._notify_auth(line)
                    self._handle_stream_event(obj)
                proc.wait(timeout=5)
                stderr_thread.join(timeout=2)
                stderr_text = "".join(stderr_chunks)
                if proc.returncode and classify_auth_error(stderr_text):
                    self._notify_auth(stderr_text)
                elif proc.returncode and not self._turn_text_sent and self._owner_chat is not None:
                    self.tg.send_message(
                        self._owner_chat,
                        f"⚠️ Codex skončil s chybou (exit {proc.returncode}) a nevrátil odpověď.",
                    )
                    self._turn_text_sent = True
                elif not self._turn_text_sent and self._owner_chat is not None:
                    self.tg.send_message(self._owner_chat, "⚠️ Codex ukončil požadavek bez odpovědi.")
                    self._turn_text_sent = True
            except Exception as e:
                log.error("stream read error: %s", e)
                if self._owner_chat is not None and not self._turn_text_sent:
                    self.tg.send_message(self._owner_chat, f"⚠️ Chyba při čtení odpovědi Codexu: {e}")
                    self._turn_text_sent = True
            finally:
                self._finish_turn()

    def _notify_auth(self, text: str) -> None:
        first = self._auth.failure(text)
        if self._owner_chat is not None and not self._turn_text_sent:
            self.tg.send_message(
                self._owner_chat,
                AUTH_REQUIRED_NOTICE if first else
                "⚠️ Codex CLI stále vyžaduje přihlášení pomocí `codex login`; "
                "požadavek nebyl proveden.",
            )
            self._turn_text_sent = True

    def _finish_turn(self) -> None:
        """Stream mode has no tmux session, transcript, or in-flight attach journal."""
        self._status_clear()
        was_active = self._turn_active.is_set()
        self._turn_active.clear()
        self._pending_turn_end = False
        if was_active:
            log.info("TURN END t=%.2f dur=%.1fs typing_fired=%d max_gap=%.2fs",
                     time.time(), time.monotonic() - self._turn_started,
                     self._typing_count, self._max_gap)

    def _handle_command(self, text: str, chat_id: int, message_id: int | None = None) -> bool:
        cmd = text.split(maxsplit=1)[0].lstrip("/").split("@")[0].lower()
        if cmd == "health":
            degraded = self._auth.status == "login_required"
            state = "working" if self._turn_active.is_set() else "idle"
            self.tg.send_message(
                chat_id,
                f"{'⚠️' if degraded else '✅'} Bridge: running\n"
                "mode: Codex stream\n"
                f"turn: {state}\n{self._auth.health_line()}",
            )
            return True
        if cmd == "diag":
            from . import __version__
            lines = [
                "Agent2Telegram diagnostics",
                f"version: `{__version__}`",
                "agent: `codex`",
                "mode: `stream`",
                f"turn active: `{self._turn_active.is_set()}`",
                *self._auth.diag_lines(),
            ]
            self.tg.send_message(chat_id, "\n".join(lines))
            return True
        return super()._handle_command(text, chat_id, message_id)

    # ---- stream event → Telegram actions ----------------------------------
    def _handle_stream_event(self, obj: dict) -> None:
        t = obj.get("type")
        if t == "thread.started":
            self._thread_id = obj.get("thread_id") or self._thread_id
            return
        item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
        itype = item.get("type")
        if t == "item.completed" and itype == "agent_message":
            out = self._strip_marker(item.get("text", ""))
            key = item.get("id") or out[:40]
            if out and self._owner_chat is not None and key not in self._sent_keys:
                self._auth.success()
                self._mark_sent(key)
                self._status_clear()               # progress/final text → drop the tool bubble
                self.tg.send_message(self._owner_chat, out)
                self._turn_text_sent = True
        elif t == "item.started" and itype and itype != "agent_message":
            iid = item.get("id")
            if iid and iid not in self._seen_tools:
                self._seen_tools.add(iid)
                self._status_push(_stream_tool_summary(item))
        elif t == "item.completed" and itype == "web_search":
            # the start had an empty query; refine the bubble once we know it
            self._status_push(_stream_tool_summary(item))
        # turn.completed handled by process exit → _finish_turn
