import json
import unittest
from unittest import mock

from avantp import logs


def run_with(stdout="", stderr="", rc=0):
    return mock.patch("avantp.logs.subprocess.run", return_value=mock.Mock(stdout=stdout, stderr=stderr, returncode=rc))


def entry(msg, prio="6", unit="avantp-timecode.service", ts=1792781000000000, ident="python3", pid="812"):
    return json.dumps({"MESSAGE": msg, "PRIORITY": prio, "_SYSTEMD_UNIT": unit, "__REALTIME_TIMESTAMP": str(ts),
                       "SYSLOG_IDENTIFIER": ident, "_PID": pid, "_HOSTNAME": "avantpserver"})


def journal(*entries):
    return "\n".join(entries) + "\n"


class TailTest(unittest.TestCase):
    def argv(self, m):
        return m.call_args[0][0]

    def test_sources_map_to_fixed_unit_lists_and_json_output(self):
        with run_with(journal(entry("a"), entry("b"))) as m:
            r = logs.tail("timecode", "all", 50, 0)
        a = self.argv(m)
        self.assertEqual(a[:2], ["journalctl", "--no-pager"])
        self.assertEqual(a[a.index("-o") + 1], "json")
        self.assertEqual(a[a.index("-u") + 1], "avantp-timecode")
        self.assertNotIn("-p", a)                                                        # priority is judged here, not by journalctl
        self.assertEqual([l.split(": ", 1)[1] for l in r["lines"]], ["a", "b"])
        self.assertRegex(r["lines"][0], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d avantpserver python3\[812\]: a$")
        with run_with("") as m:
            logs.tail("all", "all", 50, 0)
        units = [a for i, a in enumerate(self.argv(m)) if i and self.argv(m)[i - 1] == "-u"]
        self.assertIn("avantp-apply", units)
        self.assertIn("avantp-web", units)
        with run_with("") as m:
            logs.tail("kernel", "all", 50, -1)
        self.assertIn("-k", self.argv(m))
        self.assertNotIn("-u", self.argv(m))

    def test_the_level_filter_finds_warnings_that_the_journal_stored_as_info(self):
        """The bug: our services print 'WARNING ...' to stderr, systemd stores it as priority 6, so -p warning found nothing."""
        data = journal(
            entry("timecode: INFO timecode sender started"),
            entry("timecode: WARNING VLAN 50 sACN: Art-Net no-address (waiting for an IP address)"),
            entry("apply: WARNING rolling back: not confirmed within 90 s"),
            entry("timecode: ERROR unexpected error in the sender loop (failure 1 in a row)"),
            entry("Traceback (most recent call last):"),
            entry('  File "x.py", line 3, in tick'),
            entry("10.1.10.2 \"GET /api/status HTTP/1.1\" 200 -"),
            entry("login failed for 'admin' from 10.1.10.9", unit="avantp-web.service"),        # no level word, info: not shown
        )
        with run_with(data):
            warn = logs.tail("all", "warning", 100, 0)["lines"]
        text = "\n".join(warn)
        self.assertIn("Art-Net no-address", text)
        self.assertIn("rolling back", text)
        self.assertIn("unexpected error", text)
        self.assertIn("Traceback", text)
        self.assertNotIn("sender started", text)
        self.assertNotIn("GET /api/status", text)
        self.assertNotIn("File \"x.py\"", text)                                           # a plain continuation line is not an error
        with run_with(data):
            err = "\n".join(logs.tail("all", "error", 100, 0)["lines"])
        self.assertIn("unexpected error", err)
        self.assertIn("Traceback", err)
        self.assertNotIn("rolling back", err)                                              # a warning is not an error
        self.assertNotIn("Art-Net no-address", err)

    def test_real_journal_priorities_count_for_every_unit(self):
        data = journal(
            entry("carrier lost", prio="4", unit="systemd-networkd.service", ident="systemd-networkd"),
            entry("Reconfiguring", prio="6", unit="systemd-networkd.service"),
            entry("DHCP failed", prio="3", unit="systemd-networkd.service"),
            entry("something said ERROR in chrony text only", prio="6", unit="chrony.service"),      # not ours: text is ignored
            entry("kernel: Hardware Error", prio="3", unit=""),
        )
        with run_with(data):
            warn = "\n".join(logs.tail("network", "warning", 100, 0)["lines"])
        self.assertIn("carrier lost", warn)
        self.assertIn("DHCP failed", warn)
        self.assertIn("Hardware Error", warn)
        self.assertNotIn("Reconfiguring", warn)
        self.assertNotIn("chrony text only", warn)
        with run_with(data):
            err = "\n".join(logs.tail("network", "error", 100, 0)["lines"])
        self.assertIn("DHCP failed", err)
        self.assertNotIn("carrier lost", err)

    def test_a_filtered_view_looks_far_enough_back_and_returns_the_newest_matches(self):
        rows = [entry("noise %d" % i) for i in range(900)] + [entry("timecode: WARNING old problem")] + \
               [entry("noise again %d" % i) for i in range(900)] + [entry("timecode: WARNING new problem")]
        with run_with(journal(*rows)) as m:
            r = logs.tail("all", "warning", 20, 0)
        a = self.argv(m)
        self.assertGreaterEqual(int(a[a.index("-n") + 1]), 1000)                           # scans far more than the 20 shown
        self.assertEqual(len(r["lines"]), 2)
        self.assertTrue(r["lines"][-1].endswith("new problem"))
        with run_with(journal(*rows)) as m:
            logs.tail("all", "all", 50, 0)
        self.assertEqual(self.argv(m)[self.argv(m).index("-n") + 1], "50")                  # unfiltered: exactly what is shown

    def test_clear_hides_entries_up_to_a_moment_without_touching_the_journal(self):
        data = journal(entry("old a", ts=1792781000000000), entry("old b", ts=1792781100000000),
                       entry("new c", ts=1792781200000000), entry("new d", ts=1792781300000000))
        with run_with(data) as m:
            r = logs.tail("all", "all", 50, 0, since=1792781150)
        self.assertEqual([l.split(": ", 1)[1] for l in r["lines"]], ["new c", "new d"])
        self.assertTrue(all("--vacuum" not in a and "--rotate" not in a for a in m.call_args[0][0]))        # read-only
        with run_with(data):
            self.assertEqual(len(logs.tail("all", "all", 50, 0, since=0)["lines"]), 4)                   # 0 = nothing cleared
            self.assertEqual(len(logs.tail("all", "all", 50, 0, since="1792781150")["lines"]), 2)       # as it arrives from the URL
            self.assertEqual(logs.tail("all", "all", 50, 0, since=1792781400)["lines"], [])              # everything cleared
        with run_with(journal(entry("timecode: WARNING w1", ts=1792781000000000), entry("timecode: WARNING w2", ts=1792781300000000))):
            self.assertEqual(len(logs.tail("all", "warning", 50, 0, since=1792781150)["lines"]), 1)      # combines with the level

    def test_a_bad_since_is_refused(self):
        for bad in (-1, "x", "1e999", 4102444800 * 2, None):
            with run_with("") as m:
                with self.assertRaises((ValueError, TypeError)):
                    logs.tail("all", "all", 50, 0, since=bad)
            m.assert_not_called()

    def test_odd_messages_do_not_break_it(self):
        data = journal(json.dumps({"MESSAGE": [104, 105, 255], "PRIORITY": "6", "__REALTIME_TIMESTAMP": "1792781000000000"}),
                       json.dumps({"PRIORITY": "oops", "__REALTIME_TIMESTAMP": "x"}), "not json at all",
                       entry("fine"))
        with run_with(data):
            lines = logs.tail("all", "all", 50, 0)["lines"]
        self.assertTrue(any(l.endswith("fine") for l in lines))
        self.assertTrue(any("hi" in l for l in lines))

    def test_limits(self):
        with run_with("") as m:
            logs.tail("web", "all", 5, 0)
        self.assertEqual(self.argv(m)[self.argv(m).index("-n") + 1], "20")                  # clamped up
        with run_with("") as m:
            logs.tail("web", "all", 99999, 0)
        self.assertEqual(self.argv(m)[self.argv(m).index("-n") + 1], str(logs.MAX_LINES))   # clamped down

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


class PriorityPrefixTest(unittest.TestCase):
    """What our services write, so the journal stores warnings as warnings from now on."""

    def render(self, level, msg, exc=False):
        import io
        import logging
        from avantp import logfmt
        stream = io.StringIO()
        h = logging.StreamHandler(stream)
        h.setFormatter(logfmt.JournalFormatter("%(name)s: %(levelname)s %(message)s"))
        lg = logging.getLogger("prefix-test")
        lg.handlers[:] = [h]
        lg.propagate = False
        lg.setLevel(logging.DEBUG)
        if exc:
            try:
                raise RuntimeError("boom")
            except RuntimeError:
                lg.log(level, msg, exc_info=True)
        else:
            lg.log(level, msg)
        return stream.getvalue().rstrip("\n").split("\n")

    def test_levels_become_syslog_priorities(self):
        import logging
        for level, n in ((logging.INFO, 6), (logging.WARNING, 4), (logging.ERROR, 3), (logging.CRITICAL, 2), (logging.DEBUG, 7)):
            self.assertEqual(self.render(level, "x"), [f"<{n}>prefix-test: {logging.getLevelName(level)} x"])

    def test_every_line_of_a_traceback_carries_the_error_priority(self):
        import logging
        lines = self.render(logging.ERROR, "unexpected", exc=True)
        self.assertGreater(len(lines), 3)
        self.assertTrue(all(l.startswith("<3>") for l in lines), lines)
        self.assertTrue(any("Traceback" in l for l in lines))

    def test_plain_output_outside_systemd(self):
        import io
        import logging
        from avantp import logfmt
        stream = io.StringIO()
        with mock.patch.dict("os.environ", {}, clear=False):
            os_env = __import__("os").environ
            os_env.pop("INVOCATION_ID", None)
            logfmt.setup(stream)
            logging.getLogger("plain-test").warning("hello")
        self.assertEqual(stream.getvalue().strip(), "plain-test: WARNING hello")
        with mock.patch.dict("os.environ", {"INVOCATION_ID": "abc"}):
            stream2 = io.StringIO()
            logfmt.setup(stream2)
            logging.getLogger("plain-test").warning("hello")
        self.assertEqual(stream2.getvalue().strip(), "<4>plain-test: WARNING hello")


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
