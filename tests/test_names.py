import time
import unittest
from unittest import mock

from avantp import names, status


def wait_for(cond, secs=2.0):
    t = time.time()
    while time.time() - t < secs:
        if cond():
            return True
        time.sleep(0.01)
    return False


class ResolverTest(unittest.TestCase):
    def test_first_call_does_not_block_then_name_arrives(self):
        calls = []

        def slow(ip):
            calls.append(ip)
            time.sleep(0.2)
            return "ntp1.example.org"
        r = names.Resolver(lookup=slow)
        t = time.time()
        self.assertIsNone(r.get("91.206.8.70"))                    # answer immediately, lookup in the background
        self.assertLess(time.time() - t, 0.1)
        self.assertTrue(wait_for(lambda: r.get("91.206.8.70") == "ntp1.example.org"))
        r.get("91.206.8.70")
        r.get("91.206.8.70")
        self.assertEqual(calls, ["91.206.8.70"])                   # cached: one lookup only

    def test_unknown_names_are_cached_negatively(self):
        calls = []
        r = names.Resolver(lookup=lambda ip: calls.append(ip))      # returns None = no PTR record
        r.get("203.0.113.9")
        self.assertTrue(wait_for(lambda: calls))
        time.sleep(0.05)
        for _ in range(5):
            self.assertIsNone(r.get("203.0.113.9"))
        self.assertEqual(len(calls), 1)

    def test_entries_expire(self):
        clock = [1000.0]
        names_seq = iter(["old.example.org", "new.example.org"])
        r = names.Resolver(lookup=lambda ip: next(names_seq), now=lambda: clock[0])
        r.get("192.0.2.1")
        self.assertTrue(wait_for(lambda: r.get("192.0.2.1") == "old.example.org"))
        clock[0] += names.TTL + 1
        self.assertEqual(r.get("192.0.2.1"), "old.example.org")    # stale name shown while it is refreshed
        self.assertTrue(wait_for(lambda: r.get("192.0.2.1") == "new.example.org"))

    def test_garbage_input_and_failures_do_not_break_it(self):
        def boom(ip):
            raise OSError("resolver down")
        r = names.Resolver(lookup=boom)
        for junk in ("not-an-ip", "", "1.2.3", "1.2.3.4; rm -rf /", None):
            self.assertIsNone(r.get(junk))
        self.assertIsNone(r.get("192.0.2.7"))
        time.sleep(0.1)
        self.assertIsNone(r.get("192.0.2.7"))                       # worker survived the exception

    def test_getent_parsing_and_sanitising(self):
        def run(out):
            return mock.patch("avantp.names.subprocess.run", return_value=mock.Mock(stdout=out))
        with run("91.206.8.70     s1.holub.co.at  alias\n"):
            self.assertEqual(names._getent("91.206.8.70"), "s1.holub.co.at")
        with run("91.206.8.70     91.206.8.70\n"):
            self.assertIsNone(names._getent("91.206.8.70"))         # no real name, only the address again
        with run("1.2.3.4  evil<script>.example\n"):
            self.assertIsNone(names._getent("1.2.3.4"))
        with run("1.2.3.4  a.example.org.\n"):
            self.assertEqual(names._getent("1.2.3.4"), "a.example.org")
        with run(""):
            self.assertIsNone(names._getent("1.2.3.4"))
        with mock.patch("avantp.names.subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(names._getent("1.2.3.4"))             # no getent (e.g. macOS)

    def test_status_adds_host_next_to_address(self):
        r = names.Resolver(lookup=lambda ip: {"91.206.8.70": "ntp1.example.org"}.get(ip))
        r.get("91.206.8.70")
        wait_for(lambda: r.get("91.206.8.70"))
        t = {"ref_name": "91.206.8.70", "ref_id": "5B3B7164"}
        src = [{"name": "91.206.8.70"}, {"name": "203.0.113.9"}, {"name": "time.example.org"}]
        status.add_hosts(t, src, resolver=r)
        self.assertEqual(t["ref_host"], "ntp1.example.org")
        self.assertEqual([s["host"] for s in src][0], "ntp1.example.org")
        self.assertIsNone(src[1]["host"])                            # unknown yet: the UI shows the address
        self.assertEqual(src[2]["host"], "time.example.org")        # already a name: kept as is
        status.add_hosts(None, [], resolver=r)                       # chrony not running


if __name__ == "__main__":
    unittest.main()
