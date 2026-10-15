"""Test sandbox: every test gets its own workspace, Claude/Codex homes, cache and config dirs under a temp dir."""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from ticketrail import config  # noqa: E402

NOW = time.time()
TEST_CONFIG = {
    "tickets": {"patterns": [r"\b(?:ABC|AB)-\d+\b"], "url": "https://tracker.example/browse/{key}"},
    "naming": {"tags": ["OPS", "DOC"]},
    "related": {"ignore": [r"/scratchpad/", r"/plans/", r"_CHANGELOG\.md$", r"/MEMORY\.md$"]},
}


class Sandbox:
    def __init__(self, data=None, root_name="proj", tmp_parent=None):
        self.tmp = tempfile.TemporaryDirectory(dir=tmp_parent)
        base = os.path.realpath(self.tmp.name)
        self.base = base
        self.root = os.path.join(base, "work", root_name)
        os.makedirs(self.root)
        self.env = {
            "XDG_CACHE_HOME": os.path.join(base, "cache"),
            "XDG_CONFIG_HOME": os.path.join(base, "config"),
            "CLAUDE_CONFIG_DIR": os.path.join(base, "claude"),
            "CODEX_HOME": os.path.join(base, "codex"),
        }
        self.patch = mock.patch.dict(os.environ, self.env)
        self.patch.start()
        self.cfg = config.use(config.Config(root=self.root, data=config.deep_merge(config.DEFAULTS, data or {})))

    def close(self):
        self.patch.stop()
        self.tmp.cleanup()

    # --- fake transcripts ---
    def claude_dir(self, cwd=None):
        d = os.path.join(self.env["CLAUDE_CONFIG_DIR"], "projects", config.encode_path(cwd or self.root))
        os.makedirs(d, exist_ok=True)
        return d

    def claude_session(self, sid, prompts, cwd=None, title=None, files=(), answer="Done.", ts="2026-10-01T10:00:00Z"):
        cwd = cwd or self.root
        lines = []
        if title:
            lines.append({"type": "custom-title", "customTitle": title})
        for p in prompts:
            lines.append({"type": "user", "promptSource": "typed", "timestamp": ts, "cwd": cwd, "gitBranch": "main",
                          "message": {"content": p}})
        content = [{"type": "text", "text": answer}] + [
            {"type": "tool_use", "name": "Write", "input": {"file_path": f}} for f in files]
        lines.append({"type": "assistant", "timestamp": ts, "message": {"content": content}})
        path = os.path.join(self.claude_dir(cwd), sid + ".jsonl")
        with open(path, "w") as f:
            f.write("\n".join(json.dumps(x) for x in lines) + "\n")
        return path

    def codex_session(self, sid, prompts, cwd=None, patch_files=(), answer="Done.", ts="2026-10-02T10:00:00Z"):
        d = os.path.join(self.env["CODEX_HOME"], "sessions", "2026", "10", "02")
        os.makedirs(d, exist_ok=True)
        lines = [{"type": "session_meta", "payload": {"id": sid, "cwd": cwd or self.root, "timestamp": ts}}]
        for p in prompts:
            lines.append({"type": "response_item", "timestamp": ts,
                          "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": p}]}})
        for fp in patch_files:
            lines.append({"type": "response_item", "timestamp": ts, "payload": {
                "type": "custom_tool_call", "name": "apply_patch", "input": "*** Begin Patch\n*** Add File: %s\n+x\n*** End Patch" % fp}})
        lines.append({"type": "response_item", "timestamp": ts,
                      "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": answer}]}})
        path = os.path.join(d, "rollout-2026-10-02T10-00-00-%s.jsonl" % sid)
        with open(path, "w") as f:
            f.write("\n".join(json.dumps(x) for x in lines) + "\n")
        return path


class SandboxTest(unittest.TestCase):
    CONFIG = TEST_CONFIG

    def setUp(self):
        self.sb = Sandbox(self.CONFIG)
        self.root = self.sb.root

    def tearDown(self):
        self.sb.close()


class AsyncSandboxTest(unittest.IsolatedAsyncioTestCase):
    CONFIG = TEST_CONFIG

    def setUp(self):
        self.sb = Sandbox(self.CONFIG)
        self.root = self.sb.root

    def tearDown(self):
        self.sb.close()


def mk(sid, tickets=None, title_custom=None, files=(), mtime=NOW - 3600, n_prompts=3, cwd=None, tool="claude", size_kb=10,
       prompts=None):
    """An in-memory digest (what sources produce), for pure tests."""
    root = config.current().root
    prompts = prompts if prompts is not None else [{"ts": None, "text": "p"}] * n_prompts
    return {"id": sid, "tool": tool, "path": os.path.join(config.current().claude_projects, config.encode_path(root), sid + ".jsonl"),
            "title_custom": title_custom, "title_ai": None, "slug": None,
            "first_ts": "2026-10-01T00:00:00Z", "last_ts": "2026-10-0%dT00:00:00Z" % (1 + len(sid) % 9), "cwd": cwd or root,
            "branch": None, "prompts": prompts, "n_prompts": len(prompts), "tickets": dict(tickets or {}),
            "files_written": list(files), "memory_files": [], "artifacts": [], "last_assistant": "", "size_kb": size_kb,
            "mtime": mtime, "plan_file": None}
