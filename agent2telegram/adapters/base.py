"""Agent adapter abstraction.

Every supported agent is a thin adapter over its command-line tool. The bridge is
agent-agnostic: it only knows ``Adapter.run(prompt, chat_dir, is_continuation)``.

Robustness choices:
  * Commands are run via ``subprocess`` with an **argv list** (never ``shell=True``),
    so a message from Telegram can't inject shell syntax.
  * Each run has a hard timeout; a hung agent is killed, not left to block the bridge.
  * Per-chat continuity is achieved with a per-chat working directory plus the agent's
    own "continue last conversation" flag — no fragile session bookkeeping.
  * Default commands are sensible but **overridable in config**, because three external
    CLIs evolve independently and a good tool shouldn't hard-code brittle assumptions.
"""
from __future__ import annotations

import os
import signal
import shutil
import subprocess
import threading
import time
from pathlib import Path


class AdapterError(Exception):
    pass


class TaskCancelled(AdapterError):
    pass


class Adapter:
    #: Stable identifier used in config (``agent`` field).
    name: str = ""
    #: Human-friendly label for the setup wizard.
    label: str = ""
    #: Executable that must be on PATH.
    binary: str = ""
    #: argv template for the first message of a conversation. ``{prompt}`` is replaced.
    default_command: list[str] = []
    #: argv template for follow-up messages (continue the conversation). Falls back to
    #: ``default_command`` when empty (i.e. the agent has no continue mode).
    continue_command: list[str] = []
    #: Command to launch the agent's *interactive* TUI inside a tmux session (attach mode).
    #: Includes the flag that lets it run autonomously — no per-command approval prompt — since
    #: the bridge drives it unattended and only allow-listed users can reach it. Falls back to
    #: just the binary when unset.
    tui_command: list[str] = []

    @classmethod
    def tui_launch(cls) -> list[str]:
        """The full command the wizard types into a fresh tmux session to start the agent."""
        return cls.tui_command or ([cls.binary] if cls.binary else [])

    def __init__(self, *, command: list[str] | None = None,
                 continue_command: list[str] | None = None, timeout: int = 600) -> None:
        self._command = command or self.default_command
        self._continue = continue_command or self.continue_command or self._command
        self._timeout = timeout
        self._active: dict[str, subprocess.Popen] = {}
        self._cancelled: set[int] = set()
        self._active_lock = threading.Lock()

    # ---- discovery ---------------------------------------------------------
    @classmethod
    def detect(cls) -> bool:
        """True if the agent's binary is available on PATH."""
        return bool(cls.binary) and shutil.which(cls.binary) is not None

    # ---- execution ---------------------------------------------------------
    def build_argv(self, prompt: str, *, is_continuation: bool) -> list[str]:
        template = self._continue if is_continuation else self._command
        return [prompt if tok == "{prompt}" else tok.replace("{prompt}", prompt) for tok in template]

    def run(self, prompt: str, *, chat_dir: Path, is_continuation: bool) -> str:
        chat_dir.mkdir(parents=True, exist_ok=True)
        argv = self.build_argv(prompt, is_continuation=is_continuation)
        key = str(chat_dir.resolve())
        proc = None
        try:
            proc = subprocess.Popen(
                argv, cwd=str(chat_dir), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, stdin=subprocess.DEVNULL, start_new_session=True,
            )
            with self._active_lock:
                self._active[key] = proc
            try:
                stdout, stderr = proc.communicate(timeout=self._timeout)
            except subprocess.TimeoutExpired as e:
                self._terminate_group(proc)
                raise AdapterError(
                    f"{self.label or self.name} timed out after {self._timeout}s."
                ) from e
        except FileNotFoundError as e:
            raise AdapterError(
                f"'{self.binary}' not found. Is {self.label or self.name} installed and on PATH?"
            ) from e
        finally:
            if proc is not None:
                with self._active_lock:
                    self._active.pop(key, None)

        with self._active_lock:
            was_cancelled = proc.pid in self._cancelled
            self._cancelled.discard(proc.pid)
        if was_cancelled:
            raise TaskCancelled(f"{self.label or self.name} task was cancelled.")
        out = (stdout or "").strip()
        if proc.returncode != 0 and not out:
            err = (stderr or "").strip() or f"exit code {proc.returncode}"
            raise AdapterError(f"{self.label or self.name} failed: {err[:500]}")
        return self.parse_output(out)

    @staticmethod
    def _session_pids(session_id: int) -> list[int]:
        """Return same-UID processes in an OS session, including their separate process groups."""
        found = []
        uid = os.getuid()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != uid:
                    continue
                raw = (entry / "stat").read_text("utf-8")
                # comm can contain spaces/parentheses; fields after its final ')' start at state.
                fields = raw[raw.rfind(")") + 2:].split()
                if len(fields) > 3 and fields[0] != "Z" and int(fields[3]) == session_id:
                    found.append(int(entry.name))
            except (OSError, ValueError):
                continue
        return found

    @classmethod
    def _signal_session(cls, session_id: int, sig: int) -> None:
        # Descendants first, leader last: stop useful work before asking the supervising CLI to exit.
        for pid in sorted(cls._session_pids(session_id), key=lambda p: p == session_id):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass

    @classmethod
    def _terminate_group(cls, proc: subprocess.Popen, grace: float = 3.0) -> None:
        """Terminate every same-UID process in the task's isolated OS session and reap its leader."""
        session_id = proc.pid                 # start_new_session=True makes the leader its SID
        cls._signal_session(session_id, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and cls._session_pids(session_id):
            proc.poll()
            time.sleep(0.05)
        remaining = cls._session_pids(session_id)
        if remaining:
            cls._signal_session(session_id, signal.SIGKILL)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and cls._session_pids(session_id):
            time.sleep(0.05)

    def cancel(self, *, chat_dir: Path) -> bool:
        """Terminate and reap the exact process group running for one chat."""
        key = str(chat_dir.resolve())
        with self._active_lock:
            proc = self._active.get(key)
            if proc is None or proc.poll() is not None:
                return False
            self._cancelled.add(proc.pid)
        self._terminate_group(proc)
        return proc.poll() is not None and not self._session_pids(proc.pid)

    def is_active(self, *, chat_dir: Path) -> bool:
        key = str(chat_dir.resolve())
        with self._active_lock:
            proc = self._active.get(key)
            return proc is not None and proc.poll() is None

    def parse_output(self, stdout: str) -> str:
        """Hook for adapters whose CLI emits structured output. Default: raw text."""
        return stdout
