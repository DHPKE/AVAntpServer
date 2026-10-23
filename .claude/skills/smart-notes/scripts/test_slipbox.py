"""End-to-end tests for the slip-box tools: scaffold -> notes -> lint, run the way a user would (subprocess)."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run(*args, expect=0):
    r = subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True)
    assert r.returncode == expect, f"{args} exited {r.returncode} (wanted {expect})\n{r.stdout}\n{r.stderr}"
    return r


class SlipBoxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "box"
        run(HERE / "init_slipbox.py", self.root)
        self.new = lambda *a, expect=0: run(self.root / "_tools" / "new_note.py", *a, expect=expect)
        self.lint = lambda *a, expect=0: run(self.root / "_tools" / "lint.py", *a, expect=expect)

    def tearDown(self):
        self.tmp.cleanup()

    def perm_id(self, out):
        return Path(out.stdout.splitlines()[0]).name[:12]

    def test_init_scaffold_and_idempotent(self):
        for d in ("inbox", "literature", "permanent", "projects", "_templates", "_tools"):
            self.assertTrue((self.root / d).is_dir(), d)
        self.assertTrue((self.root / "README.md").exists() and (self.root / "index.md").exists())
        (self.root / "README.md").write_text("my edits")
        r = run(HERE / "init_slipbox.py", self.root)
        self.assertEqual((self.root / "README.md").read_text(), "my edits")   # never overwritten
        self.assertIn("kept", r.stdout)

    def test_fleeting_and_processed(self):
        a = self.new("fleeting", "Notes should be written for strangers")
        b = self.new("fleeting", "Notes should be written for strangers")       # same words, same minute
        fa, fb = Path(a.stdout.strip()), Path(b.stdout.strip())
        self.assertNotEqual(fa, fb)
        self.assertIn("strangers", fa.read_text())
        self.new("processed", fa)
        self.assertTrue((self.root / "inbox" / ".processed" / fa.name).exists() and not fa.exists())
        self.new("processed", self.root / "README.md", expect=1)                  # only inbox files

    def test_permanent_requires_a_link_after_the_first(self):
        first = self.new("permanent", "Writing is the medium of thinking", "--body", "x")
        a = self.perm_id(first)
        self.new("permanent", "A second idea with no connection at all", expect=1)
        self.new("permanent", "Bad link", "--link", "999999999999", expect=1)
        time.sleep(0.01)
        second = self.new("permanent", "Reading without writing leaves no trace", "--link", f"{a}:supports", "--source", "@ahrens2017")
        b = self.perm_id(second)
        self.assertNotEqual(a, b)
        first_text = next((self.root / "permanent").glob(f"{a}-*.md")).read_text()
        self.assertIn(f"[[{b}]]", first_text, "back-link must be written into the linked note")
        self.assertIn(f"[[{a}]] - supports", next((self.root / "permanent").glob(f"{b}-*.md")).read_text())

    def test_index_adds_and_extends_an_entry(self):
        a = self.perm_id(self.new("permanent", "Notes are the unit of thinking here", "--body", "x"))
        self.new("index", "Method", a)
        self.new("index", "Method", a)                                            # idempotent
        text = (self.root / "index.md").read_text()
        self.assertEqual(text.count(f"[[{a}]]"), 1)
        self.new("index", "Method", "123456789012", expect=1)

    def healthy_box(self):
        a = self.perm_id(self.new("permanent", "Writing is the medium of thinking itself", "--body", "Short idea."))
        b = self.perm_id(self.new("permanent", "Reading alone leaves no lasting trace", "--link", f"{a}:supports", "--body", "Short."))
        self.new("index", "Thinking", a)
        return a, b

    def test_lint_clean_box_passes(self):
        self.healthy_box()
        r = self.lint()
        self.assertIn("OK", r.stdout)

    def test_lint_finds_the_rule_breaks(self):
        a, b = self.healthy_box()
        perm = self.root / "permanent"
        # broken link + a note that links to nothing
        (perm / "202601010101-lonely-note-with-no-links-at-all.md").write_text(
            "---\nid: 202601010101\n---\n# Lonely note with no links at all\n\ntext\n\n## Connections\n- [[999999999999]]\n")
        # too long + keyword title + orphan (nothing leads to it, but it links out)
        (perm / "202601010102-topic.md").write_text(
            f"---\nid: 202601010102\n---\n# Topic\n\n{'word ' * 400}\n\n## Connections\n- [[{a}]]\n")
        # stale fleeting note and unused literature note
        old = self.root / "inbox" / "old.md"
        old.write_text("captured: long ago\n")
        os.utime(old, (time.time() - 10 * 86400,) * 2)
        lit = self.root / "literature" / "@unused.md"
        lit.write_text("# @unused\n")
        os.utime(lit, (time.time() - 30 * 86400,) * 2)
        r = self.lint(expect=1)
        out = r.stdout
        self.assertIn("broken link [[999999999999]]", out)
        self.assertIn("links to no other permanent note", out)
        self.assertIn("more than one idea", out)
        self.assertIn("names a topic", out)
        self.assertIn("orphan", out)
        self.assertIn("inbox/old.md", out)
        self.assertIn("@unused.md", out)
        self.assertIn("FAILED", out)

    def test_lint_flags_duplicate_ids_and_bad_index(self):
        a, _ = self.healthy_box()
        (self.root / "permanent" / f"{a}-copy-of-the-same-idea-again.md").write_text("# Copy of the same idea again\n")
        (self.root / "index.md").write_text(f"# Index\n\n## Entry points\n- **X** → [[111111111111]]\n")
        out = self.lint(expect=1).stdout
        self.assertIn(f"duplicate id {a}", out)
        self.assertIn("missing note [[111111111111]]", out)

    def test_tools_work_from_the_skill_folder_with_root_flag(self):
        r = run(HERE / "new_note.py", "--root", self.root, "fleeting", "from outside")
        self.assertTrue(Path(r.stdout.strip()).exists())
        run(HERE / "lint.py", "--root", self.root)
        run(HERE / "lint.py", "--root", self.tmp.name, expect=1)                    # not a slip-box -> clear failure


if __name__ == "__main__":
    unittest.main(verbosity=1)
