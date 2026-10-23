import copy
import json
import os
import tempfile
import threading
import time
import unittest

from avantp import apply, config
from tests.test_avantp import BASE


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.p = apply.Paths(self.root)
        os.makedirs(self.p.data)
        os.makedirs(self.p.run)
        self.ex = apply.FakeExecutor()

    def pend(self, cfg=None):
        with open(os.path.join(self.p.data, "pending.json"), "w") as f:
            json.dump(cfg or BASE, f)
        open(os.path.join(self.p.data, "apply.request"), "w").close()

    def status(self):
        try:
            return json.load(open(os.path.join(self.p.run, "status.json")))
        except (FileNotFoundError, ValueError):
            return {}

    def run_apply(self, confirm, secs=3):
        """confirm=True: a 'user' presses Confirm shortly after; False: nobody does."""
        if confirm:
            def press():
                while self.status().get("state") != "pending-confirm":
                    time.sleep(0.02)
                open(os.path.join(self.p.data, "confirm"), "w").close()
            threading.Thread(target=press, daemon=True).start()
        return apply.apply_once(self.p, self.ex, confirm_seconds=secs, poll=0.02)

    def f(self, rel):
        return os.path.join(self.root, rel)

    def test_happy_path(self):
        self.pend()
        self.assertEqual(self.run_apply(True), "applied")
        self.assertEqual(self.status()["state"], "applied")
        self.assertIn("table inet avantp", open(self.f("etc/avantp/nftables.nft")).read())
        self.assertTrue(os.path.exists(self.f("etc/systemd/network/30-avantp-enp3s0.10.netdev")))
        self.assertEqual(json.load(open(self.f("etc/avantp/config.json")))["vlans"][0]["id"], 10)
        self.assertFalse(os.path.exists(os.path.join(self.p.data, "apply.request")))
        self.assertFalse(os.path.exists(os.path.join(self.p.data, "confirm")))

    def test_active_config_only_written_after_confirm(self):
        self.pend()
        self.run_apply(False, secs=0.3)
        self.assertFalse(os.path.exists(self.f("etc/avantp/config.json")))

    def test_unconfirmed_change_rolls_back_files(self):
        os.makedirs(self.f("etc/chrony"))
        open(self.f("etc/chrony/chrony.conf"), "w").write("ORIGINAL\n")
        self.pend()
        self.assertEqual(self.run_apply(False, secs=0.3), "rolled-back")
        s = self.status()
        self.assertEqual(s["state"], "rolled-back")
        self.assertIn("not confirmed", s["reason"])
        self.assertEqual(open(self.f("etc/chrony/chrony.conf")).read(), "ORIGINAL\n")          # restored
        self.assertFalse(os.path.exists(self.f("etc/avantp/nftables.nft")))                       # did not exist before
        self.assertFalse(os.path.exists(self.f("etc/systemd/network/30-avantp-enp3s0.10.netdev")))
        self.assertIn(("rollback", False), self.ex.calls)

    def test_activation_failure_rolls_back_immediately(self):
        self.ex = apply.FakeExecutor(fail_activate=True)
        self.pend()
        t = time.time()
        self.assertEqual(self.run_apply(False, secs=30), "rolled-back")
        self.assertLess(time.time() - t, 5)
        self.assertIn("activation failed", self.status()["reason"])
        self.assertFalse(os.path.exists(self.f("etc/avantp/nftables.nft")))

    def test_invalid_pending_changes_nothing(self):
        bad = copy.deepcopy(BASE)
        bad["vlans"][1]["id"] = 10
        self.pend(bad)
        self.assertEqual(self.run_apply(False), "error")
        self.assertIn("duplicate vlan id", self.status()["error"])
        self.assertFalse(os.path.exists(self.f("etc/avantp/nftables.nft")))
        self.assertEqual(self.ex.calls, [])

    def test_preflight_failure_changes_nothing(self):
        self.ex = apply.FakeExecutor(fail_preflight=True)
        self.pend()
        self.assertEqual(self.run_apply(False), "error")
        self.assertIn("nothing changed", self.status()["error"])
        self.assertFalse(os.path.exists(self.f("etc/avantp/nftables.nft")))

    def test_symlinked_pending_is_refused(self):
        secret = self.f("secret.json")
        json.dump(BASE, open(secret, "w"))
        os.symlink(secret, os.path.join(self.p.data, "pending.json"))
        open(os.path.join(self.p.data, "apply.request"), "w").close()
        self.assertEqual(self.run_apply(False), "error")
        self.assertFalse(os.path.exists(self.f("etc/avantp/nftables.nft")))

    def test_oversize_pending_is_refused(self):
        self.pend({"x": "a" * (apply.MAX_JSON + 10)})
        self.assertEqual(self.run_apply(False), "error")

    def test_removed_vlan_is_deleted_and_rolled_back_with_its_files(self):
        self.pend()
        self.run_apply(True)
        smaller = copy.deepcopy(BASE)
        smaller["vlans"] = smaller["vlans"][:1]
        self.pend(smaller)
        self.ex.calls.clear()
        self.assertEqual(self.run_apply(True), "applied")
        self.assertFalse(os.path.exists(self.f("etc/systemd/network/30-avantp-enp3s0.20.netdev")))
        act = [c for c in self.ex.calls if isinstance(c, tuple) and c[0] == "activate"][0]
        self.assertEqual(set(act[1]), {"enp3s0.20", "enp3s0.30"})        # interfaces to delete
        # a third, unconfirmed change brings the removed VLAN's files back
        self.pend()
        self.assertEqual(self.run_apply(False, secs=0.3), "rolled-back")
        self.assertFalse(os.path.exists(self.f("etc/systemd/network/30-avantp-enp3s0.20.netdev")))  # state before it
        self.assertEqual(json.load(open(self.f("etc/avantp/config.json")))["vlans"].__len__(), 1)

    def test_takeover_and_its_rollback(self):
        os.makedirs(self.f("etc"))
        os.symlink("../run/systemd/resolve/stub-resolv.conf", self.f("etc/resolv.conf"))
        self.ex = apply.FakeExecutor(takeover=True)
        self.ex.extra_paths = lambda: ["etc/resolv.conf"]
        self.pend()
        self.assertEqual(self.run_apply(False, secs=0.3), "rolled-back")
        self.assertIn("takeover", self.ex.calls)
        self.assertIn(("rollback", True), self.ex.calls)
        self.assertEqual(os.readlink(self.f("etc/resolv.conf")), "../run/systemd/resolve/stub-resolv.conf")


if __name__ == "__main__":
    unittest.main()
