"""The repository must not carry anything specific to the workspace it was extracted from."""
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# split literals ("ab" "c") are joined by Python, so this file never contains the words it forbids
FORBIDDEN = [re.compile(p, re.I) for p in (
    "vex" "ere", "quang" "le",  # origin workspace and user
    r"\bV" r"RD?-\d", r"\bT" r"XM\b", r"\bT" r"XDL\b",  # origin ticket keys and domain terms
    "/Us" "ers/", "/var/fol" "ders/",  # absolute home and macOS temp paths
)]
SKIP_DIRS = {".git", "__pycache__", ".venv", "dist", "build"}


class TestNoHardcoding(unittest.TestCase):
    def test_no_workspace_specific_strings(self):
        hits = []
        for d, dirs, files in os.walk(REPO):
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.endswith(".egg-info")]
            for f in files:
                path = os.path.join(d, f)
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    text = fh.read()
                for rx in FORBIDDEN:
                    for m in rx.finditer(text):
                        hits.append("%s:%d %s" % (os.path.relpath(path, REPO), text.count("\n", 0, m.start()) + 1, m.group(0)))
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
