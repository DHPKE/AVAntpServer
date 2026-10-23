import copy
import json
import os
import tempfile
import unittest
import urllib.error

from avantp import apply, config, software
from tests.test_avantp import BASE

MAIN = "b47ea20" + "0" * 33


def gh(main=MAIN, compare=None, fail=None):
    """A stand-in for the GitHub API."""
    calls = []

    def fetch(path, timeout=10):
        calls.append(path)
        if fail:
            raise fail
        if path == "/commits/main":
            return {"sha": main, "commit": {"message": "Newest thing\n\nlonger text", "committer": {"date": "2026-10-23T21:00:00Z"}}}
        if path.startswith("/compare/"):
            if isinstance(compare, Exception):
                raise compare
            return compare or {"status": "ahead", "ahead_by": 2}
        raise AssertionError(path)
    fetch.calls = calls
    return fetch


class CompareTest(unittest.TestCase):
    def test_up_to_date(self):
        r = software.compare("b47ea20", gh())
        self.assertEqual((r["state"], r["latest"]), ("uptodate", "b47ea20"))
        self.assertEqual(r["message"], "Newest thing")                              # first line only

    def test_newer_commits_on_github(self):
        f = gh(compare={"status": "ahead", "ahead_by": 3})
        r = software.compare("0021bf5", f)
        self.assertEqual((r["state"], r["ahead_by"]), ("newer", 3))
        self.assertIn("/compare/0021bf5...", f.calls[1])

    def test_installed_version_unknown_to_github_is_a_local_build(self):
        err = urllib.error.HTTPError("u", 404, "not found", {}, None)
        self.assertEqual(software.compare("deadbee", gh(compare=err))["state"], "local-build")

    def test_dirty_or_unknown_versions_are_local_builds_without_a_second_request(self):
        for v in ("0021bf5-dirty", "unknown", "", "v1.0"):
            f = gh()
            self.assertEqual(software.compare(v, f)["state"], "local-build", v)
            self.assertEqual(f.calls, ["/commits/main"])

    def test_installed_is_newer_or_diverged(self):
        self.assertEqual(software.compare("0021bf5", gh(compare={"status": "behind", "ahead_by": 0}))["state"], "local-newer")
        self.assertEqual(software.compare("0021bf5", gh(compare={"status": "diverged", "ahead_by": 1}))["state"], "diverged")


class HandleTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.p = apply.Paths(self.root)
        for d in (self.p.data, self.p.run, os.path.join(self.root, "opt/avantp")):
            os.makedirs(d, exist_ok=True)
        self.ex = apply.FakeExecutor()
        self.set_installed("0021bf5")
        os.makedirs(self.p.etc, exist_ok=True)
        json.dump(config.dump(config.validate(copy.deepcopy(BASE))), open(os.path.join(self.p.etc, "config.json"), "w"))

    def set_installed(self, v):
        open(os.path.join(self.root, "opt/avantp/VERSION"), "w").write(v)

    def request(self, word):
        open(os.path.join(self.p.data, "software.request"), "w").write(word)

    def status(self):
        return json.load(open(os.path.join(self.p.run, "software.json")))

    def windows(self):
        return [c[2] for c in self.ex.calls if isinstance(c, tuple) and c[0] == "update_window"]

    def good_runner(self):
        seen = []

        def runner(on_line):
            on_line(["first run: cloning x"])
            seen.append(self.status()["step"])
            on_line(["first run: cloning x", "code: 0021bf5 -> b47ea20", "installing version b47ea20"])
            seen.append(self.status()["step"])
            self.set_installed("b47ea20")
            return 0, ["code: 0021bf5 -> b47ea20", "installed version b47ea20: chronyd ..."]
        runner.seen = seen
        return runner

    def test_check_opens_the_window_only_for_the_request_and_closes_it(self):
        self.request("check")
        self.assertEqual(software.handle(self.p, self.ex, fetch=gh(compare={"status": "ahead", "ahead_by": 2})), "checked")
        st = self.status()
        self.assertEqual((st["state"], st["check"]["state"], st["check"]["ahead_by"], st["installed"]), ("checked", "newer", 2, "0021bf5"))
        self.assertEqual(self.windows(), [True, False])                                 # opened, then closed again
        self.assertFalse(os.path.exists(os.path.join(self.p.data, "software.request")))

    def test_no_internet_gives_a_clear_error_and_the_window_is_closed(self):
        self.request("check")
        self.assertEqual(software.handle(self.p, self.ex, fetch=gh(fail=urllib.error.URLError("timed out"))), "error")
        self.assertIn("Could not reach GitHub", self.status()["error"])
        self.assertEqual(self.windows(), [True, False])

    def test_a_private_repository_is_explained_not_called_a_network_problem(self):
        for code in (404, 401, 403):
            self.request("check")
            err = urllib.error.HTTPError("u", code, "x", {}, None)
            self.assertEqual(software.handle(self.p, self.ex, fetch=gh(fail=err)), "error")
            msg = self.status()["error"]
            self.assertIn(f"GitHub answered {code}", msg)
            self.assertIn("private", msg)
            self.assertIn("deploy key", msg)
            self.assertNotIn("Could not reach", msg)
            self.assertEqual(self.windows()[-2:], [True, False])                          # the window was still closed
        self.request("check")
        software.handle(self.p, self.ex, fetch=gh(fail=urllib.error.HTTPError("u", 503, "x", {}, None)))
        self.assertIn("HTTP 503", self.status()["error"])

    def test_update_when_already_current_changes_nothing(self):
        self.set_installed("b47ea20")
        self.request("update")
        ran = []
        self.assertEqual(software.handle(self.p, self.ex, fetch=gh(), runner=lambda cb: ran.append(1) or (0, [])), "uptodate")
        self.assertEqual(ran, [])
        self.assertEqual(self.windows(), [True, False])

    def test_update_runs_the_script_reports_progress_and_the_new_version(self):
        self.request("update")
        runner = self.good_runner()
        self.assertEqual(software.handle(self.p, self.ex, fetch=gh(), runner=runner), "done")
        st = self.status()
        self.assertEqual((st["state"], st["from_version"], st["installed"]), ("done", "0021bf5", "b47ea20"))
        self.assertEqual(runner.seen, ["Downloading", "Installing"])                    # the step follows the script's output
        self.assertEqual(self.windows(), [True, False])
        self.assertEqual(self.windows().count(True), 1)

    def test_a_failing_update_reports_the_reason_and_still_closes_the_window(self):
        self.request("update")
        runner = lambda cb: (2, ["Could not reach GitHub/Debian. Is Update mode ON?"])
        self.assertEqual(software.handle(self.p, self.ex, fetch=gh(), runner=runner), "error")
        st = self.status()
        self.assertIn("update failed", st["error"])
        self.assertIn("Update mode", st["error"])
        self.assertEqual(self.windows(), [True, False])

    def test_a_crashing_runner_cannot_leave_the_window_open(self):
        self.request("update")

        def boom(cb):
            raise OSError("no such file")
        self.assertEqual(software.handle(self.p, self.ex, fetch=gh(), runner=boom), "error")
        self.assertIn("could not be run", self.status()["error"])
        self.assertEqual(self.windows(), [True, False])

    def test_only_two_words_are_accepted_and_symlinks_refused(self):
        for bad in ("", "UPDATE", "update; reboot", "check\nupdate", "restart"):
            self.request(bad)
            self.assertEqual(software.handle(self.p, self.ex, fetch=gh(), runner=lambda cb: (0, [])), "error", bad)
        self.assertEqual(self.windows(), [])
        secret = os.path.join(self.root, "x")
        open(secret, "w").write("update")
        os.symlink(secret, os.path.join(self.p.data, "software.request"))
        self.assertEqual(software.handle(self.p, self.ex, fetch=gh()), "error")

    def test_refused_while_an_apply_waits_for_confirmation(self):
        json.dump({"state": "pending-confirm"}, open(os.path.join(self.p.run, "status.json"), "w"))
        self.request("update")
        self.assertEqual(software.handle(self.p, self.ex, fetch=gh(), runner=lambda cb: (0, [])), "error")
        self.assertIn("apply", self.status()["error"])
        self.assertEqual(self.windows(), [])

    def test_without_an_active_config_there_is_no_firewall_to_open(self):
        os.unlink(os.path.join(self.p.etc, "config.json"))
        self.request("check")
        self.assertEqual(software.handle(self.p, self.ex, fetch=gh()), "checked")
        self.assertEqual(self.windows(), [])

    def test_the_last_check_is_remembered_through_an_update(self):
        self.request("check")
        software.handle(self.p, self.ex, fetch=gh())
        self.request("update")
        software.handle(self.p, self.ex, fetch=gh(), runner=self.good_runner())
        self.assertEqual(self.status()["check"]["latest"], "b47ea20")

    def test_step_labels(self):
        self.assertEqual(software.step_of(["git is not installed: installing it..."]), "Installing git")
        self.assertEqual(software.step_of(["first run: cloning x"]), "Downloading")
        self.assertEqual(software.step_of(["code: a -> b"]), "Downloaded")
        self.assertEqual(software.step_of(["code: a -> b", "installing version b"]), "Installing")
        self.assertEqual(software.step_of(["installed version b: chronyd"]), "Done")


if __name__ == "__main__":
    unittest.main()
