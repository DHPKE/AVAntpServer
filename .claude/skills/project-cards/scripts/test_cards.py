"""End-to-end tests for the card board tools, run like a user would (subprocess)."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run(*args, expect=0):
    r = subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True)
    assert r.returncode == expect, f"{args} exited {r.returncode} (wanted {expect})\n{r.stdout}\n{r.stderr}"
    return r


class CardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "cards"
        run(HERE / "init_cards.py", self.root, "--project", "demo")
        self.c = lambda *a, expect=0: run(self.root / "_tools" / "cards.py", *a, expect=expect)

    def tearDown(self):
        self.tmp.cleanup()

    def card(self, status, name):
        return self.root / "demo" / status / f"{name}.md"

    def test_init_scaffold_and_idempotent(self):
        for s in ("now", "waiting", "later", "done"):
            self.assertTrue((self.root / "demo" / s).is_dir())
        self.assertTrue((self.root / "README.md").exists() and (self.root / "_tools" / "cards.py").exists())
        (self.root / "README.md").write_text("mine")
        self.assertIn("kept", run(HERE / "init_cards.py", self.root, "--project", "demo").stdout)
        self.assertEqual((self.root / "README.md").read_text(), "mine")

    def test_new_move_log_ruling(self):
        self.c("new", "Release v1.2 builds green", "--project", "demo", "--status", "now", "--next", "tag after CI", "--goal", "3 platforms green")
        f = self.card("now", "release-v1-2-builds-green")
        self.assertTrue(f.exists())
        self.assertIn("status: now", f.read_text())
        self.c("new", "Release v1.2 builds green", "--project", "demo", expect=1)                      # duplicate name
        self.c("move", "release-v1-2-builds-green", "waiting", expect=1)                              # needs waiting_on
        self.c("move", "release-v1-2-builds-green", "waiting", "--waiting-on", "user: which tag")
        g = self.card("waiting", "release-v1-2-builds-green")
        self.assertTrue(g.exists() and not f.exists())
        text = g.read_text()
        self.assertIn("status: waiting", text)
        self.assertIn("waiting_on: user: which tag", text)
        self.assertIn("moved now -> waiting", text)
        self.c("log", "release-v1-2-builds-green", "asked the user")
        self.c("ruling", "demo/release-v1-2-builds-green", "keep version 0.0.1")
        text = g.read_text()
        self.assertRegex(text, r"## Log[^\n]*\n- \d{4}-\d\d-\d\d asked the user")                    # newest first, right below the heading
        self.assertRegex(text, r"## Rulings[^\n]*\n- \d{4}-\d\d-\d\d keep version 0.0.1")
        self.c("move", "release-v1-2-builds-green", "done")
        done = self.card("done", "release-v1-2-builds-green")
        self.assertTrue(done.exists())
        self.assertNotIn("waiting_on", done.read_text())                                              # cleared when it stops waiting

    def test_waiting_card_requires_waiting_on_at_creation(self):
        self.c("new", "Needs hardware", "--project", "demo", "--status", "waiting", expect=1)
        self.c("new", "Needs hardware", "--project", "demo", "--status", "waiting", "--waiting-on", "board arrives")
        self.assertTrue(self.card("waiting", "needs-hardware").exists())

    def test_list_shows_open_cards_and_hides_done_by_default(self):
        self.c("new", "Active task", "--project", "demo", "--status", "now", "--next", "do it")
        self.c("new", "Blocked task", "--project", "demo", "--status", "waiting", "--waiting-on", "user")
        self.c("new", "Old task", "--project", "demo", "--status", "done")
        out = self.c("list").stdout
        self.assertIn("Active task", out)
        self.assertIn("waiting on: user", out)
        self.assertNotIn("Old task", out)
        self.assertIn("Old task", self.c("list", "--all").stdout)

    def test_check_flags_inconsistencies(self):
        self.c("new", "Fine card", "--project", "demo", "--status", "now", "--next", "go")
        self.assertIn("OK", self.c("check").stdout)
        # a card dragged into the wrong folder by hand, a waiting card without waiting_on, a stale now card
        (self.root / "demo" / "later" / "fine-card.md").write_text(self.card("now", "fine-card").read_text())
        self.card("now", "fine-card").rename(self.root / "demo" / "waiting" / "fine-card-2.md")
        stale = self.root / "demo" / "now" / "stale.md"
        stale.write_text("---\ncard: Stale\nproject: demo\nstatus: now\nnext: x\nupdated: 2020-01-01\n---\n## Log\n")
        out = self.c("check", expect=1).stdout
        self.assertIn("status is 'now' but the card is in later/", out)
        self.assertIn("waiting card without waiting_on", out)
        self.assertIn("untouched for", out)
        self.assertIn("FAILED", out)

    def test_ambiguous_names_need_a_project(self):
        self.c("new", "Same name", "--project", "demo", "--status", "now", "--next", "a")
        self.c("new", "Same name", "--project", "other", "--status", "now", "--next", "b")
        self.c("log", "same-name", "x", expect=1)
        self.c("log", "other/same-name", "x")

    def test_works_from_the_skill_folder_with_root(self):
        run(HERE / "cards.py", "--root", self.root, "new", "Remote card", "--project", "demo", "--status", "later", "--next", "x")
        self.assertIn("Remote card", run(HERE / "cards.py", "--root", self.root, "list", "--all").stdout)


if __name__ == "__main__":
    unittest.main(verbosity=1)
