"""Tests for the agent adapters (argv building, registry, overrides)."""
import tempfile
import os
import subprocess
import threading
import time
import unittest
from pathlib import Path

from agent2telegram import adapters
from agent2telegram.adapters import AdapterError, build
from agent2telegram.adapters.claude_code import ClaudeCodeAdapter
from agent2telegram.adapters.codex import CodexAdapter
from agent2telegram.config import Config


class AdapterArgvTests(unittest.TestCase):
    def test_claude_first_turn(self):
        a = ClaudeCodeAdapter()
        argv = a.build_argv("hello world", is_continuation=False)
        self.assertEqual(argv[0], "claude")
        self.assertIn("hello world", argv)
        self.assertNotIn("--continue", argv)

    def test_claude_continuation_uses_continue_flag(self):
        a = ClaudeCodeAdapter()
        argv = a.build_argv("again", is_continuation=True)
        self.assertIn("--continue", argv)
        self.assertIn("again", argv)

    def test_codex_continuation_uses_resume_subcommand(self):
        a = CodexAdapter()
        argv = a.build_argv("again", is_continuation=True)
        self.assertEqual(argv[:4], ["codex", "exec", "resume", "--last"])
        self.assertIn("again", argv)

    def test_prompt_with_spaces_is_a_single_argv_token(self):
        # Critical: the message must be ONE argv element (no shell, no injection).
        a = CodexAdapter()
        argv = a.build_argv("rm -rf / ; echo pwned", is_continuation=False)
        self.assertIn("rm -rf / ; echo pwned", argv)

    def test_embedded_prompt_placeholder_substituted(self):
        a = adapters.GenericAdapter(command=["tool", "--msg={prompt}"])
        argv = a.build_argv("hi", is_continuation=False)
        self.assertEqual(argv, ["tool", "--msg=hi"])


class RegistryTests(unittest.TestCase):
    def test_build_known_agent(self):
        cfg = Config(agent="claude-code", token="1:2", allowed_user_ids=[1])
        self.assertIsInstance(build(cfg), ClaudeCodeAdapter)

    def test_build_unknown_agent_raises(self):
        cfg = Config(agent="does-not-exist", token="1:2", allowed_user_ids=[1])
        with self.assertRaises(AdapterError):
            build(cfg)

    def test_generic_requires_command(self):
        cfg = Config(agent="generic", token="1:2", allowed_user_ids=[1], command=None)
        with self.assertRaises(AdapterError):
            build(cfg)

    def test_command_override_applies(self):
        cfg = Config(agent="codex", token="1:2", allowed_user_ids=[1],
                     command=["codex", "exec", "--model", "gpt-5.5", "{prompt}"])
        a = build(cfg)
        argv = a.build_argv("x", is_continuation=False)
        self.assertIn("--model", argv)


class ManagedProcessTests(unittest.TestCase):
    def test_stdin_is_immediate_eof(self):
        with tempfile.TemporaryDirectory() as td:
            a = adapters.GenericAdapter(
                command=["python3", "-c", "import sys; print(len(sys.stdin.read()))"], timeout=5
            )
            self.assertEqual(a.run("ignored", chat_dir=Path(td), is_continuation=False), "0")

    def test_cancel_terminates_and_reaps_exact_chat_process(self):
        with tempfile.TemporaryDirectory() as td:
            chat = Path(td)
            a = adapters.GenericAdapter(
                command=["python3", "-c", "import time; time.sleep(60)"], timeout=65
            )
            result = []

            def run():
                try:
                    a.run("ignored", chat_dir=chat, is_continuation=False)
                except Exception as exc:
                    result.append(type(exc).__name__)

            worker = threading.Thread(target=run)
            worker.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if a.cancel(chat_dir=chat):
                    break
                time.sleep(0.02)
            worker.join(timeout=3)

            self.assertFalse(worker.is_alive())
            self.assertEqual(result, ["TaskCancelled"])

    def test_sigkill_escalation_reaps_process_ignoring_sigterm(self):
        proc = subprocess.Popen(
            ["python3", "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        time.sleep(0.05)
        adapters.GenericAdapter._terminate_group(proc, grace=0.05)
        proc.communicate(timeout=1)
        self.assertIsNotNone(proc.returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(proc.pid, 0)

    def test_two_chat_processes_are_isolated(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            chat_a, chat_b = root / "a", root / "b"
            a = adapters.GenericAdapter(
                command=["python3", "-c", "import time; time.sleep(60)"], timeout=65
            )
            outcomes = []

            def run(chat):
                try:
                    a.run("ignored", chat_dir=chat, is_continuation=False)
                except Exception as exc:
                    outcomes.append((chat.name, type(exc).__name__))

            threads = [threading.Thread(target=run, args=(chat,)) for chat in (chat_a, chat_b)]
            for thread in threads:
                thread.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if a.is_active(chat_dir=chat_a) and a.is_active(chat_dir=chat_b):
                    break
                time.sleep(0.02)
            self.assertTrue(a.cancel(chat_dir=chat_a))
            self.assertTrue(a.is_active(chat_dir=chat_b))
            self.assertTrue(a.cancel(chat_dir=chat_b))
            for thread in threads:
                thread.join(timeout=3)
            self.assertCountEqual(outcomes, [("a", "TaskCancelled"), ("b", "TaskCancelled")])

    def test_cancel_removes_child_in_separate_process_group_same_session(self):
        with tempfile.TemporaryDirectory() as td:
            chat = Path(td)
            pid_file = chat / "child.pid"
            code = (
                "import os,pathlib,subprocess,time; "
                "p=subprocess.Popen(['sleep','60'], preexec_fn=os.setpgrp); "
                f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); "
                "time.sleep(60)"
            )
            a = adapters.GenericAdapter(command=["python3", "-c", code], timeout=65)
            result = []

            def run():
                try:
                    a.run("ignored", chat_dir=chat, is_continuation=False)
                except Exception as exc:
                    result.append(type(exc).__name__)

            worker = threading.Thread(target=run)
            worker.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not pid_file.exists():
                time.sleep(0.02)
            child_pid = int(pid_file.read_text("utf-8"))
            self.assertTrue(a.cancel(chat_dir=chat))
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result, ["TaskCancelled"])
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)


class TuiLaunchTests(unittest.TestCase):
    """The wizard launches the interactive TUI with the autonomous flag, so the bridge can drive
    the agent unattended — otherwise a fresh install blocks on an approval prompt nobody can see."""

    def test_codex_launches_with_bypass_flag(self):
        self.assertEqual(CodexAdapter.tui_launch(),
                         ["codex", "--dangerously-bypass-approvals-and-sandbox"])

    def test_claude_launches_with_skip_permissions(self):
        self.assertEqual(ClaudeCodeAdapter.tui_launch(),
                         ["claude", "--dangerously-skip-permissions"])


if __name__ == "__main__":
    unittest.main()
