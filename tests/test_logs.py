import json
import unittest
from unittest import mock

from avantp import logs


def run_with(stdout="", stderr="", rc=0):
    return mock.patch("avantp.logs.subprocess.run", return_value=mock.Mock(stdout=stdout, stderr=stderr, returncode=rc))


class TailTest(unittest.TestCase):
    def argv(self, m):
        return m.call_args[0][0]

    def test_sources_map_to_fixed_unit_lists(self):
        with run_with("a\nb\n") as m:
            r = logs.tail("timecode", "all", 50, 0)
        a = self.argv(m)
        self.assertEqual(a[:2], ["journalctl", "--no-pager"])
        self.assertEqual(a[a.index("-u") + 1], "avantp-timecode")
        self.assertEqual(r, {"lines": ["a", "b"], "error": None})
        with run_with("") as m:
            logs.tail("all", "all", 50, 0)
        units = [a for i, a in enumerate(self.argv(m)) if i and self.argv(m)[i - 1] == "-u"]
        self.assertIn("avantp-apply", units)
        self.assertIn("avantp-web", units)
        with run_with("") as m:
            logs.tail("kernel", "all", 50, -1)
        self.assertIn("-k", self.argv(m))
        self.assertNotIn("-u", self.argv(m))

    def test_levels_and_limits(self):
        with run_with("") as m:
            logs.tail("web", "warning", 5, 0)
        a = self.argv(m)
        self.assertEqual(a[a.index("-p") + 1], "warning")
        self.assertEqual(a[a.index("-n") + 1], "20")                         # clamped up
        with run_with("") as m:
            logs.tail("web", "error", 99999, 0)
        a = self.argv(m)
        self.assertEqual(a[a.index("-p") + 1], "err")
        self.assertEqual(a[a.index("-n") + 1], str(logs.MAX_LINES))          # clamped down

    def test_nothing_from_the_request_reaches_journalctl_as_an_option(self):
        for src, lvl, n, boot in (("web; rm -rf /", "all", 50, 0), ("--vacuum-size=1", "all", 50, 0), ("all", "emerg", 50, 0),
                                  ("all", "all", "x", 0), ("all", "all", 50, 1), ("all", "all", 50, -99), ("all", "all", 50, "-b")):
            with run_with("") as m:
                with self.assertRaises((ValueError, TypeError)):
                    logs.tail(src, lvl, n, boot)
            m.assert_not_called()

    def test_missing_journal_access_gives_a_helpful_message(self):
        with run_with("", "Hint: You are currently not seeing messages from other users and the system.\nNo journal files were opened due to insufficient permissions.", 1):
            r = logs.tail("web", "all", 50, 0)
        self.assertEqual(r["lines"], [])
        self.assertIn("systemd-journal", r["error"])
        with mock.patch("avantp.logs.subprocess.run", side_effect=FileNotFoundError):
            self.assertIn("not available", logs.tail("web", "all", 50, 0)["error"])

    def test_separator_lines_are_dropped(self):
        with run_with("-- Boot abc --\nOct 23 20:55:35 x line\n") as m:
            self.assertEqual(logs.tail("web", "all", 50, 0)["lines"], ["Oct 23 20:55:35 x line"])


class BootsTest(unittest.TestCase):
    def setUp(self):
        logs._boots_cache = (0.0, None)

    def test_clean_and_abrupt_endings(self):
        listing = json.dumps([
            {"index": -2, "boot_id": "a", "first_entry": 1_000_000_000_000, "last_entry": 1_000_100_000_000},
            {"index": -1, "boot_id": "b", "first_entry": 1_000_200_000_000, "last_entry": 1_000_300_000_000},
            {"index": 0, "boot_id": "c", "first_entry": 1_000_400_000_000, "last_entry": 1_000_500_000_000}])

        def fake(argv, **kw):
            if "--list-boots" in argv:
                return mock.Mock(stdout=listing, stderr="", returncode=0)
            idx = argv[argv.index("-b") + 1]
            text = {"-2": "...\nReached target reboot.target - System Reboot.\nsystemd-shutdown[1]: Syncing", "-1": "chrony: Selected source\nStarted avantp-timecode.service"}[idx]
            return mock.Mock(stdout=text, stderr="", returncode=0)
        with mock.patch("avantp.logs.subprocess.run", side_effect=fake):
            r = logs.boots()
        self.assertEqual([b["ended"] for b in r["boots"]], ["clean", "abrupt", "running"])
        self.assertEqual(r["boots"][0]["first"], 1_000_000)
        with mock.patch("avantp.logs.subprocess.run", side_effect=AssertionError("cache must be used")):
            self.assertEqual(logs.boots(), r)                                 # cached for a few seconds

    def test_errors_are_reported(self):
        with run_with("", "No journal files were opened due to insufficient permissions.", 1):
            self.assertIn("systemd-journal", logs.boots()["error"])


if __name__ == "__main__":
    unittest.main()
