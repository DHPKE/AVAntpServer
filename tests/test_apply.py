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


class ChangedOnlyTest(unittest.TestCase):
    """A reconfigure drops a link's DHCP lease and may give the box a different address: only touch what changed."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.p = apply.Paths(self.root)
        os.makedirs(self.p.data)
        os.makedirs(self.p.run)
        self.ex = apply.FakeExecutor()

    def run_apply(self, cfg):
        json.dump(cfg, open(os.path.join(self.p.data, "pending.json"), "w"))

        def press():
            while not os.path.exists(os.path.join(self.p.run, "status.json")) or \
                    json.load(open(os.path.join(self.p.run, "status.json"))).get("state") != "pending-confirm":
                time.sleep(0.02)
            open(os.path.join(self.p.data, "confirm"), "w").close()
        threading.Thread(target=press, daemon=True).start()
        return apply.apply_once(self.p, self.ex, confirm_seconds=3, poll=0.02)

    def test_reapplying_the_same_config_changes_nothing(self):
        self.assertEqual(self.run_apply(BASE), "applied")
        self.assertIn("etc/systemd/network/10-avantp-wan.network", self.ex.last_changed)       # first time: everything is new
        self.assertEqual(self.run_apply(BASE), "applied")
        self.assertEqual(self.ex.last_changed, set())

    def test_a_vlan_edit_does_not_touch_the_wan(self):
        self.run_apply(BASE)
        edited = copy.deepcopy(BASE)
        edited["vlans"][0]["address"] = "10.10.10.3/24"
        self.run_apply(edited)
        net = {r for r in self.ex.last_changed if r.startswith("etc/systemd/network/")}
        self.assertEqual(net, {"etc/systemd/network/30-avantp-enp3s0.10.network"})     # no WAN file, no other VLAN
        self.assertIn("etc/avantp/nftables.nft", self.ex.last_changed)                  # the firewall pins the address, so it changes too

    def test_system_executor_reconfigures_only_changed_links(self):
        ex = apply.SystemExecutor(self.p)
        ran = []
        ex.sh = lambda *argv, check=True, timeout=60: ran.append(argv)
        cfg = config.validate(copy.deepcopy(BASE))
        ex.activate(cfg, [], False, {"etc/systemd/network/30-avantp-enp3s0.10.network",
                                     "etc/systemd/network/30-avantp-enp3s0.10.netdev", "etc/avantp/nftables.nft"})
        reconf = [a[2] for a in ran if a[:2] == ("networkctl", "reconfigure")]
        self.assertEqual(reconf, ["enp3s0.10"])                       # not enp4s0 (WAN), not the other VLANs
        self.assertNotIn(("systemctl", "restart", "chrony"), ran)    # chrony.conf unchanged
        ran.clear()
        ex.activate(cfg, [], False, set())
        self.assertEqual([a for a in ran if a[0] == "networkctl" and a[1] == "reconfigure"], [])
        ran.clear()
        ex.activate(cfg, [], False, {"etc/systemd/network/10-avantp-wan.network", "etc/chrony/chrony.conf"})
        self.assertEqual([a[2] for a in ran if a[:2] == ("networkctl", "reconfigure")], ["enp4s0"])
        self.assertIn(("systemctl", "restart", "chrony"), ran)
        ran.clear()
        ex.activate(cfg, [], True, set())                              # takeover: everything is configured from scratch
        self.assertEqual(len([a for a in ran if a[:2] == ("networkctl", "reconfigure")]), 2 + len(cfg["vlans"]))

    def test_rollback_reconfigures_only_changed_links(self):
        ex = apply.SystemExecutor(self.p)
        ran = []
        ex.sh = lambda *argv, check=True, timeout=60: ran.append(argv)
        old = config.validate(copy.deepcopy(BASE))
        ex.rollback(False, old, {"etc/systemd/network/30-avantp-enp3s0.20.network"})
        self.assertEqual([a[2] for a in ran if a[:2] == ("networkctl", "reconfigure")], ["enp3s0.20"])


class UpdateModeTest(unittest.TestCase):
    def setUp(self):
        from avantp import updatemode
        self.um = updatemode
        self.root = tempfile.mkdtemp()
        self.p = apply.Paths(self.root)
        os.makedirs(self.p.data)
        os.makedirs(self.p.run)
        self.ex = apply.FakeExecutor()

    def active(self):
        os.makedirs(self.p.etc, exist_ok=True)
        json.dump(config.dump(config.validate(copy.deepcopy(BASE))), open(os.path.join(self.p.etc, "config.json"), "w"))

    def request(self, text):
        open(os.path.join(self.p.data, "update.request"), "w").write(text)

    def status(self):
        return json.load(open(os.path.join(self.p.run, "update.json")))

    def test_on_and_off(self):
        self.active()
        self.request("on")
        self.assertEqual(self.um.handle(self.p, self.ex, now=lambda: 1000.0), "on")
        self.assertEqual(self.status()["until"], 1000.0 + 900)
        self.assertIn(("update_window", "enp4s0", True, 900), self.ex.calls)      # WAN interface from the ACTIVE config
        self.request("off")
        self.assertEqual(self.um.handle(self.p, self.ex), "off")
        self.assertEqual(self.status()["state"], "off")
        self.assertIn(("update_window", "enp4s0", False, 900), self.ex.calls)
        self.assertFalse(os.path.exists(os.path.join(self.p.data, "update.request")))

    def test_only_the_two_words_are_accepted(self):
        self.active()
        for bad in ("", "ON", "on; nft flush ruleset", "enp9s0", "on\noff", "1"):
            self.request(bad)
            self.assertEqual(self.um.handle(self.p, self.ex), "error", bad)
        self.assertEqual(self.ex.calls, [])

    def test_refused_without_an_active_config(self):
        self.request("on")
        self.assertEqual(self.um.handle(self.p, self.ex), "error")
        self.assertIn("apply one first", self.status()["error"])
        self.assertEqual(self.ex.calls, [])

    def test_symlinked_request_is_refused(self):
        self.active()
        secret = os.path.join(self.root, "x")
        open(secret, "w").write("on")
        os.symlink(secret, os.path.join(self.p.data, "update.request"))
        self.assertEqual(self.um.handle(self.p, self.ex), "error")
        self.assertEqual(self.ex.calls, [])

    def test_executor_failure_is_reported(self):
        self.active()
        self.ex.update_window = lambda *a: (_ for _ in ()).throw(RuntimeError("nft: no such set"))
        self.request("on")
        self.assertEqual(self.um.handle(self.p, self.ex), "error")
        self.assertIn("no such set", self.status()["error"])

    def test_an_apply_closes_an_open_window_in_the_status(self):
        self.active()
        self.request("on")
        self.um.handle(self.p, self.ex)
        self.assertEqual(self.status()["state"], "on")
        self.um.close_after_apply(self.p)
        self.assertEqual(self.status()["state"], "off")

    def test_system_executor_commands(self):
        ex = apply.SystemExecutor(self.p)
        ran = []
        ex.sh = lambda *argv, check=True, timeout=60: ran.append((argv, check))
        ex.update_window("enp4s0", True, 900)
        ex.update_window("enp4s0", False, 900)
        self.assertEqual(ran[0][0], ("nft", "add", "element", "inet", "avantp", "update_window", '{ "enp4s0" timeout 900s }'))
        self.assertEqual(ran[1][0][:5], ("nft", "delete", "element", "inet", "avantp"))
        self.assertFalse(ran[1][1])                  # closing an already-closed window is not an error


class PreflightTest(unittest.TestCase):
    def test_chrony_candidate_is_readable_by_the_unprivileged_chronyd_and_cleaned_up(self):
        import stat
        from unittest import mock
        from avantp import render
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "etc/chrony"))
        ex = apply.SystemExecutor(apply.Paths(root))
        seen = {}

        def fake_run(argv, **kw):
            if argv[0] == "chronyd":
                path = argv[-1]
                seen["path"], seen["mode"] = path, stat.S_IMODE(os.stat(path).st_mode)
                seen["dir_mode"] = stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode)
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("avantp.apply.subprocess.run", fake_run):
            ok, why = ex.preflight(render.render_all(config.validate(copy.deepcopy(BASE))))
        self.assertTrue(ok, why)
        self.assertEqual(os.path.dirname(seen["path"]), os.path.join(root, "etc/chrony"))   # where AppArmor allows reads
        self.assertTrue(seen["mode"] & 0o044 and seen["dir_mode"] & 0o055)                    # others can read + traverse
        self.assertEqual(os.listdir(os.path.join(root, "etc/chrony")), [])                    # nothing left behind

    def test_failure_text_is_reported(self):
        from unittest import mock
        from avantp import render
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "etc/chrony"))
        ex = apply.SystemExecutor(apply.Paths(root))
        files = render.render_all(config.validate(copy.deepcopy(BASE)))
        with mock.patch("avantp.apply.subprocess.run",
                        lambda argv, **kw: mock.Mock(returncode=1 if argv[0] == "chronyd" else 0, stdout="", stderr="bad directive")):
            ok, why = ex.preflight(files)
        self.assertFalse(ok)
        self.assertIn("bad directive", why)
        self.assertEqual(os.listdir(os.path.join(root, "etc/chrony")), [])


class TakeoverTest(unittest.TestCase):
    def test_takeover_hands_dns_to_resolved_and_disables_ifupdown(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "etc"))
        open(os.path.join(root, "etc/resolv.conf"), "w").write("nameserver 8.8.8.8\n")
        ex = apply.SystemExecutor(apply.Paths(root))
        ran = []
        ex.sh = lambda *argv, check=True, timeout=60: ran.append(argv)
        ex.takeover(config.validate(copy.deepcopy(BASE)))
        self.assertIn(("systemctl", "disable", "networking"), ran)
        self.assertIn(("ifdown", "--force", "enp4s0"), ran)
        self.assertIn(("systemctl", "enable", "--now", "systemd-resolved"), ran)
        self.assertEqual(os.readlink(os.path.join(root, "etc/resolv.conf")), "../run/systemd/resolve/stub-resolv.conf")
        # ifupdown goes away only AFTER nothing else can fail halfway: networkd is enabled last
        self.assertEqual(ran[-1], ("systemctl", "enable", "systemd-networkd"))

    def test_rollback_of_takeover_gives_the_network_back_to_ifupdown(self):
        ex = apply.SystemExecutor(apply.Paths(tempfile.mkdtemp()))
        ran = []
        ex.sh = lambda *argv, check=True, timeout=60: ran.append(argv)
        ex.rollback(True, {"wan": {"iface": "enp4s0"}})
        flat = [" ".join(a) for a in ran]
        self.assertIn("systemctl disable --now systemd-networkd systemd-resolved", flat)
        self.assertIn("systemctl enable networking", flat)
        self.assertIn("ip addr flush dev enp4s0", flat)
        self.assertIn("systemctl restart networking", flat)


if __name__ == "__main__":
    unittest.main()
