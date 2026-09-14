"""Behavioural tests for the bridge — no network, no real agent."""
import tempfile
import unittest

from agent2telegram import stt
from agent2telegram.bridge import Bridge, Task
from agent2telegram.config import Config


class _FakeClient:
    def __init__(self):
        self.sent = []
        self.actions = []
        self.files = {}            # file_id -> bytes

    def get_me(self):
        return {"username": "fakebot"}

    def send_chat_action(self, chat_id, action="typing"):
        self.actions.append((chat_id, action))

    def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))

    # attachment support
    def get_file_path(self, file_id):
        return f"path/{file_id}.bin"

    def download(self, file_path, timeout=120):
        return b"FILE-BYTES"


class _FakeAdapter:
    def __init__(self):
        self.calls = []

    def run(self, prompt, *, chat_dir, is_continuation):
        chat_dir.mkdir(parents=True, exist_ok=True)
        self.calls.append({"prompt": prompt, "is_continuation": is_continuation})
        return f"echo: {prompt}"

    def cancel(self, *, chat_dir):
        return False

    def is_active(self, *, chat_dir):
        return False


class BridgeTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Config(agent="claude-code", token="1:2", allowed_user_ids=[7], workdir=self.tmp.name)
        self.bridge = Bridge(cfg, client=_FakeClient())
        self.adapter = _FakeAdapter()
        self.bridge.adapter = self.adapter

    def tearDown(self):
        self.tmp.cleanup()

    def sent_texts(self):
        return [t for _, t in self.bridge.tg.sent]


class ContinuityTests(BridgeTestBase):
    def test_first_turn_is_not_continuation_second_is(self):
        self.bridge.process(100, Task(text="hello"))
        self.bridge.process(100, Task(text="again"))
        self.assertFalse(self.adapter.calls[0]["is_continuation"])
        self.assertTrue(self.adapter.calls[1]["is_continuation"])

    def test_reply_is_sent(self):
        self.bridge.process(100, Task(text="hello"))
        self.assertEqual(self.bridge.tg.sent[-1], (100, "echo: hello"))

    def test_reset_makes_next_turn_fresh(self):
        self.bridge.process(100, Task(text="hello"))
        self.bridge._reset_chat(100)
        self.bridge.process(100, Task(text="after reset"))
        self.assertFalse(self.adapter.calls[-1]["is_continuation"])

    def test_separate_chats_are_independent(self):
        self.bridge.process(1, Task(text="a"))
        self.bridge.process(2, Task(text="b"))
        self.assertFalse(self.adapter.calls[0]["is_continuation"])
        self.assertFalse(self.adapter.calls[1]["is_continuation"])

    def test_cancel_reports_only_verified_result(self):
        self.adapter.cancel = lambda **_kwargs: True
        handled = self.bridge._handle_command(100, 7, "/cancel")
        self.assertTrue(handled)
        self.assertIn("skutečně ukončena", self.sent_texts()[-1])


class AuthTests(BridgeTestBase):
    def test_unauthorized_user_is_refused(self):
        self.bridge._dispatch({"update_id": 1, "message": {
            "chat": {"id": 100}, "from": {"id": 999}, "text": "do something"}})
        self.assertEqual(self.adapter.calls, [])
        self.assertTrue(any("not authorized" in t.lower() for t in self.sent_texts()))


class AttachmentTests(BridgeTestBase):
    def test_image_is_downloaded_and_attached(self):
        task = self.bridge._build_task(100, {"photo": [{"file_id": "small"}, {"file_id": "big"}]}, "look")
        self.assertIsNotNone(task.attachment)
        self.assertTrue(task.attachment.endswith("image.jpg"))
        with open(task.attachment, "rb") as f:
            self.assertEqual(f.read(), b"FILE-BYTES")
        self.assertEqual(task.text, "look")

    def test_document_is_downloaded(self):
        task = self.bridge._build_task(
            100, {"document": {"file_id": "d1", "file_name": "report.pdf"}}, "")
        self.assertTrue(task.attachment.endswith("report.pdf"))

    def test_attachment_path_is_added_to_prompt(self):
        self.bridge.process(100, Task(text="describe", attachment="/tmp/pic.jpg"))
        self.assertIn("/tmp/pic.jpg", self.adapter.calls[-1]["prompt"])
        self.assertIn("describe", self.adapter.calls[-1]["prompt"])

    def test_unsafe_filename_is_sanitized(self):
        task = self.bridge._build_task(
            100, {"document": {"file_id": "d", "file_name": "../../etc/passwd"}}, "")
        self.assertNotIn("/", task.attachment.split("attachments/")[-1])


class VoiceTests(BridgeTestBase):
    def test_voice_without_key_is_disabled(self):
        task = self.bridge._build_task(100, {"voice": {"file_id": "v1"}}, "")
        self.assertIsNone(task)
        self.assertTrue(any("aren't enabled" in t for t in self.sent_texts()))

    def test_voice_with_key_is_transcribed(self):
        self.bridge._stt_key = "fake-key"
        orig = stt.transcribe
        stt.transcribe = lambda audio, **kw: "hello from voice"
        try:
            task = self.bridge._build_task(100, {"voice": {"file_id": "v1"}}, "")
        finally:
            stt.transcribe = orig
        self.assertIsNotNone(task)
        self.assertEqual(task.text, "hello from voice")


if __name__ == "__main__":
    unittest.main()
