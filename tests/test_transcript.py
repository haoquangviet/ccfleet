"""Transcript reader: prompts typed while a session is busy, and the files a session touched."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _setup  # noqa: F401
import fleet_transcript  # noqa: E402


def _write(path: Path, entries: list[dict]) -> None:
    # first line is skipped by the reader (it may be cut), so lead with a filler
    path.write_text("\n".join(json.dumps(e) for e in [{"type": "x"}] + entries) + "\n", encoding="utf-8")


class Transcript(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccfleet-tr-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.proj = self.tmp / "proj"
        (self.proj / "out").mkdir(parents=True)
        (self.proj / "out" / "demo.mp4").write_bytes(b"x" * 10)
        (self.proj / "notes.md").write_text("n")
        self.paste = self.tmp / "paste"
        self.paste.mkdir()
        (self.paste / "paste-1.png").write_bytes(b"p")
        (self.tmp / "secret.png").write_bytes(b"s")
        self.jsonl = self.tmp / "s.jsonl"
        patcher = mock.patch.object(fleet_transcript, "find", return_value=self.jsonl)
        patcher.start()
        self.addCleanup(patcher.stop)
        fleet_transcript._scan_cache.clear()

    def test_prompt_sent_while_busy_is_shown(self):
        """Prompts typed mid-turn are queue-operation/attachment entries, never `user` entries."""
        _write(self.jsonl, [
            {"type": "queue-operation", "operation": "enqueue", "content": "absorbed one"},
            {"type": "queue-operation", "operation": "remove", "content": "absorbed one"},
            {"type": "attachment", "attachment": {"type": "queued_command", "prompt": "absorbed one"}},
            {"type": "queue-operation", "operation": "enqueue", "content": "still waiting"},
            {"type": "queue-operation", "operation": "enqueue", "content": "<task-notification>x"},
        ])
        items = fleet_transcript.tail({}, "s", 20)
        self.assertEqual([(i["text"], bool(i.get("queued"))) for i in items],
                         [("absorbed one", False), ("still waiting", True)])

    def test_files_written_and_sent_inside_project_only(self):
        _write(self.jsonl, [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Write", "input": {"file_path": str(self.proj / "notes.md")}},
                {"type": "text", "text": f"video at out/demo.mp4 and {self.tmp / 'secret.png'}"}]}},
            {"type": "user", "message": {"content": f"[image: {self.paste / 'paste-1.png'}] look"}},
        ])
        got = {i["rel"]: (i["via"], i["src"], i["kind"])
               for i in fleet_transcript.files({}, "s", str(self.proj), paste_dir=str(self.paste))}
        self.assertEqual(got, {"notes.md": ("write", "project", "file"),
                               "out/demo.mp4": ("text", "project", "video"),
                               "paste-1.png": ("user", "paste", "image")})

    def test_project_files_spans_older_conversations(self):
        """A new session still lists the files earlier conversations produced."""
        import re
        d = self.tmp / "cfg" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(self.proj))
        d.mkdir(parents=True)
        _write(d / "old.jsonl", [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": str(self.proj / "notes.md")}}]}}])
        _write(d / "new.jsonl", [{"type": "assistant", "message": {"content": [
            {"type": "text", "text": "see out/demo.mp4"}]}}])
        accs = {"main": {"config_dir": str(self.tmp / "cfg")}}
        with mock.patch.object(fleet_transcript, "find", return_value=d / "new.jsonl"):
            got = {i["rel"]: i["via"] for i in fleet_transcript.project_files(accs, str(self.proj), "new")}
        self.assertEqual(got, {"notes.md": "write", "out/demo.mp4": "text"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
