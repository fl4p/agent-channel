import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "codex" / "channel" / "scripts" / "channel.py"


class ChannelHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.channel_dir = Path(self.tmp.name)
        self.env = os.environ.copy()
        self.env["CHANNEL_DIR"] = str(self.channel_dir)
        for key in (
            "CLAUDE_CHANNEL_IID",
            "CLAUDE_CODE_SESSION_ID",
            "CLAUDE_CODE_CHILD_SESSION",
            "CODEX_SESSION_ID",
            "OPENCODE_SESSION_ID",
        ):
            self.env.pop(key, None)

    def run_helper(self, *args, input_text=None):
        return subprocess.run(
            [sys.executable, str(HELPER), *args],
            env=self.env,
            input=input_text,
            text=True,
            capture_output=True,
            check=True,
            timeout=5,
        )

    def start_stream(self, channel="demo", agent="me"):
        return subprocess.Popen(
            [sys.executable, "-u", str(HELPER), "stream", channel, agent],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def wait_for_cursor(self, channel, agent, expected):
        cursor = self.channel_dir / f"{channel}.{agent}.cursor"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if cursor.exists() and cursor.read_text().strip() == str(expected):
                return cursor
            time.sleep(0.01)
        self.fail(f"cursor did not reach {expected}: {cursor}")

    def test_leave_stops_stream_without_deleting_cursor_or_replaying(self):
        self.run_helper("setup", "demo", "me")
        self.run_helper("send", "demo", "peer", "old-message")
        self.run_helper("poll", "demo", "me", "--timeout", "0")

        stream = self.start_stream()
        self.addCleanup(lambda: stream.kill() if stream.poll() is None else None)
        self.run_helper("send", "demo", "peer", "current-message")
        cursor = self.wait_for_cursor("demo", "me", 2)

        self.run_helper("leave", "demo", "me")
        stdout, stderr = stream.communicate(timeout=5)

        self.assertEqual(stream.returncode, 0, stderr)
        self.assertIn("[peer] current-message", stdout)
        self.assertNotIn("old-message", stdout)
        self.assertTrue(cursor.exists())
        self.assertEqual(cursor.read_text().strip(), "2")

        self.run_helper("send", "demo", "peer", "after-leave")
        self.run_helper("setup", "demo", "me")
        self.assertEqual(cursor.read_text().strip(), "4")

    def test_peer_leave_does_not_stop_persistent_stream(self):
        self.run_helper("setup", "demo", "me")
        stream = self.start_stream()
        self.addCleanup(lambda: stream.kill() if stream.poll() is None else None)

        self.run_helper("leave", "demo", "peer")
        self.wait_for_cursor("demo", "me", 1)
        self.assertIsNone(stream.poll())

        self.run_helper("leave", "demo", "me")
        stdout, stderr = stream.communicate(timeout=5)
        self.assertEqual(stream.returncode, 0, stderr)
        self.assertIn("[peer] left the channel", stdout)
        self.assertIn("[stream: a peer left the channel]", stdout)

    def test_stdin_send_preserves_shell_metacharacters_as_text(self):
        message = "use `rg` and $(uname) * [x]\nnext"
        self.run_helper("send", "safe", "alice", "--stdin", input_text=message)

        record = json.loads((self.channel_dir / "safe.ndjson").read_text())
        self.assertEqual(record["text"], "use `rg` and $(uname) * [x] next")


if __name__ == "__main__":
    unittest.main()
