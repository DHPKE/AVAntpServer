import os
import tempfile
import unittest

from avantp import heal, syncstate

TRACK = {"ref_id": "25FCBD75", "stratum": 3, "ref_time": 1000.0, "leap": "Normal"}


def src(state, reach):
    return {"state": state, "name": "a" if state == "*" else "b", "reach": reach}


class SyncState(unittest.TestCase):
    def test_selected_source_is_synced(self):
        s = syncstate.sync_state(TRACK, [src("*", 0o377), src("+", 0o377), src("?", 0)], 1030.0)
        self.assertEqual((s["state"], s["selected"], s["reachable"], s["total"], s["last_sync_age_s"]), ("synced", "a", 2, 3, 30.0))

    def test_stale_tracking_without_a_selected_source_is_not_synced(self):
        # what the box showed after three days offline: stratum 3, a real reference, leap "Normal", all sources reach 0
        s = syncstate.sync_state(TRACK, [src("?", 0), src("?", 0)], 1000.0 + 74 * 3600)
        self.assertEqual((s["state"], s["selected"], s["reachable"]), ("unsynced", None, 0))

    def test_own_local_clock_is_not_synced(self):
        s = syncstate.sync_state(dict(TRACK, ref_id=syncstate.LOCAL_REF, stratum=10), [src("?", 0)], 1010.0)
        self.assertEqual(s["state"], "local")

    def test_chrony_says_not_synchronised(self):
        s = syncstate.sync_state(dict(TRACK, leap="Not synchronised", ref_id="00000000", stratum=0), [src("*", 1)], 1010.0)
        self.assertEqual(s["state"], "unsynced")

    def test_no_connection_beats_a_still_selected_source(self):
        # unplugged: chrony keeps the source selected for hours, the probe knows within seconds
        s = syncstate.sync_state(TRACK, [src("*", 0o377)], 1010.0, {"connected": False})
        self.assertEqual(s["state"], "offline")
        self.assertEqual(syncstate.sync_state(TRACK, [src("*", 0o377)], 1010.0, {"connected": True})["state"], "synced")
        self.assertEqual(syncstate.sync_state(TRACK, [src("*", 0o377)], 1010.0, None)["state"], "synced")   # no watchdog: chrony alone

    def test_wan_file_is_ignored_when_missing_or_stale(self):
        with tempfile.TemporaryDirectory() as d:
            p = d + "/status.json"
            self.assertIsNone(syncstate.read_wan(100.0, p))
            with open(p, "w") as f:
                f.write('{"t": 100.0, "connected": false}')
            self.assertEqual(syncstate.read_wan(110.0, p)["connected"], False)
            self.assertIsNone(syncstate.read_wan(100.0 + syncstate.WAN_FRESH_S + 1, p))
            with open(p, "w") as f:
                f.write("not json")
            self.assertIsNone(syncstate.read_wan(100.0, p))

    def test_no_tracking_is_stopped(self):
        self.assertEqual(syncstate.sync_state(None, [], 0)["state"], "stopped")

    def test_never_synchronised_has_no_age(self):
        self.assertIsNone(syncstate.sync_state(dict(TRACK, ref_time=0.0), [], 5.0)["last_sync_age_s"])


if __name__ == "__main__":
    unittest.main()


class Heal(unittest.TestCase):
    def test_nudges_when_the_route_comes_back_then_backs_off_and_stops_when_synced(self):
        h = heal.Healer()
        self.assertFalse(h.step("unsynced", False, 0))           # offline: nothing to reach
        self.assertFalse(h.step("unsynced", False, 100))
        self.assertTrue(h.step("unsynced", True, 200))           # WAN back: at once
        self.assertFalse(h.step("unsynced", True, 215))
        self.assertTrue(h.step("unsynced", True, 231))           # +30 s
        self.assertFalse(h.step("unsynced", True, 270))
        self.assertTrue(h.step("unsynced", True, 292))           # +60 s
        gaps, t = [], 292
        for _ in range(6):
            t += heal.TICK
            while not h.step("unsynced", True, t):
                t += heal.TICK
            gaps.append(h.next_at - t)
        self.assertEqual(max(gaps), heal.MAX)
        self.assertFalse(h.step("synced", True, t + 1))          # synced: quiet again, next outage starts at FIRST
        self.assertTrue(h.step("local", True, t + 2))

    def test_route_flapping_nudges_each_time_it_returns(self):
        h = heal.Healer()
        self.assertTrue(h.step("unsynced", True, 0))
        self.assertFalse(h.step("unsynced", False, 5))
        self.assertTrue(h.step("unsynced", True, 10))

    def test_link_needs_two_lost_probes_to_go_down_and_one_answer_to_come_back(self):
        link, up = heal.Link(), ["1.1.1.1", "2.2.2.2", "3.3.3.3"]
        asked = []
        ok = lambda s: asked.append(s) or True
        lost = lambda s: asked.append(s) or False
        self.assertTrue(link.step(True, up, ok))
        self.assertTrue(link.step(True, up, lost))               # one lost packet is not an outage
        self.assertFalse(link.step(True, up, lost))
        self.assertTrue(link.step(True, up, ok))                 # one answer and it is back
        self.assertEqual(asked, up + ["1.1.1.1"])                # a different server each time
        self.assertFalse(link.step(False, up, ok))               # no default route: down at once, nothing sent
        self.assertEqual(len(asked), 4)
        self.assertFalse(link.step(True, [], ok))                # no servers known: the last verdict stands

    def test_holdover_counts_from_the_last_sync_and_ends_when_synced_again(self):
        h = heal.Holdover(1000.0)
        self.assertEqual(h.step("synced", 2, 5000.0), (True, None))                      # synced: the count starts again here
        self.assertEqual(h.step("offline", 2, 5000.0 + 3600), (True, 5000.0 + 7200))
        self.assertEqual(h.step("offline", 2, 5000.0 + 7199), (True, 5000.0 + 7200))
        self.assertEqual(h.step("unsynced", 2, 5000.0 + 7200), (False, 5000.0 + 7200))   # connected but not selected counts as no upstream
        self.assertEqual(h.step("offline", None, 5000.0 + 9999), (True, None))           # no limit set: never stop
        self.assertEqual(h.step("offline", 5, 5000.0 + 9999), (True, 5000.0 + 18000))    # raising the value reopens
        self.assertEqual(h.step("synced", 2, 5000.0 + 20000), (True, None))
        for state in ("offline", "unsynced", "local"):                                   # 0 hours: stop the moment it is not synced
            self.assertEqual(h.step(state, 0, 30000.0), (False, h.last_ok))
        self.assertEqual(h.step("synced", 0, 31000.0), (True, None))                     # and serve again as soon as it is

    def test_access_denies_every_look_while_stopped_and_restores_the_rules_once(self):
        a, rules = heal.Access(), ["allow 10.10.10.0/24", "allow 10.20.0.0/24"]
        self.assertEqual(a.step(True, rules), [("allow", "10.10.10.0/24"), ("allow", "10.20.0.0/24")])   # first look: make sure it is open
        self.assertEqual(a.step(True, rules), [])
        self.assertEqual(a.step(False, rules), [("deny", "all")])
        self.assertEqual(a.step(False, rules), [("deny", "all")])                        # again: a chronyd restart must not reopen it
        self.assertEqual(a.step(True, rules), [("allow", "10.10.10.0/24"), ("allow", "10.20.0.0/24")])
        self.assertEqual(a.step(True, rules), [])
        self.assertEqual(heal.Access().step(True, None), [])                             # no config read yet: restore nothing

    def test_settings_follow_the_active_config_and_keep_the_last_good_one(self):
        import json
        from tests.test_avantp import BASE
        with tempfile.TemporaryDirectory() as d:
            p = d + "/config.json"
            st = heal.Settings(p)
            st.reload()
            self.assertEqual((st.hours, st.access), (None, None))                           # no file yet
            cfg = json.loads(json.dumps(BASE))
            cfg["upstream"]["holdover_hours"] = 12
            with open(p, "w") as f:
                json.dump(cfg, f)
            st.reload()
            self.assertEqual(st.hours, 12)
            self.assertTrue(all(r.startswith("allow ") for r in st.access))
            with open(p, "w") as f:
                f.write("{broken")
            st.reload()
            self.assertEqual(st.hours, 12)

    def test_events_log_losses_as_warnings_and_recoveries_as_info(self):
        import logging
        base = {"connected": True, "reason": "", "state": "synced", "selected": "1.2.3.4", "reachable": 17, "total": 17, "age": 12.0, "serving": True}
        self.assertEqual([l for l, _ in heal.events(None, base)], [logging.INFO, logging.INFO])           # first look: where we stand
        self.assertEqual(heal.events(base, dict(base)), [])                                               # no change, no line
        down = dict(base, connected=False, reason="no default route", state="offline", age=7200.0)
        ev = heal.events(base, down)
        self.assertEqual([l for l, _ in ev], [logging.WARNING, logging.WARNING])
        self.assertIn("connection to the internet lost: no default route", ev[0][1])
        self.assertIn("no connection; last sync 2.0 h ago", ev[1][1])
        self.assertNotIn("reachable", ev[1][1])                                                           # chronyd's reach register lags a lost link
        back = heal.events(down, dict(base, state="unsynced", selected=None))
        self.assertEqual([l for l, _ in back], [logging.INFO, logging.WARNING])                           # online again, not yet selected
        self.assertIn("no upstream server selected; 17 of 17 sources reachable, last sync", back[1][1])   # here the count is meaningful
        self.assertEqual([l for l, _ in heal.events(dict(base, state="unsynced"), base)], [logging.INFO])
        held = heal.events(down, dict(down, serving=False))
        self.assertEqual([l for l, _ in held], [logging.WARNING])
        self.assertIn("NTP, Art-Net and MTC are stopped", held[0][1])
        self.assertIn("never synchronised", heal.events(None, dict(down, age=None))[1][1])

    def test_ago_picks_a_readable_unit(self):
        self.assertEqual([heal.ago(x) for x in (12, 89, 300, 5399, 7200, 171000, 269648)],
                         ["12 s", "89 s", "5 min", "90 min", "2.0 h", "47.5 h", "3.1 d"])

    def test_one_bad_look_does_not_end_the_watchdog(self):
        from unittest import mock
        with mock.patch.object(heal.status, "chrony", side_effect=[RuntimeError("boom"), KeyboardInterrupt]) as ch, \
             mock.patch.object(heal.time, "sleep"), mock.patch.object(heal, "log") as log:
            with self.assertRaises(KeyboardInterrupt):               # only this ends the loop: the first, failed look did not
                heal.main("/nonexistent/config.json")
        self.assertEqual(ch.call_count, 2)
        log.exception.assert_called_once()

    def test_watchdog_asks_chronyd_to_resolve_its_servers_once_the_network_is_there(self):
        from unittest import mock
        none = {"tracking": None, "sources": [], "sync": {"state": "offline", "selected": None, "reachable": 0, "total": 0,
                                                          "last_sync_age_s": None, "serving": True, "stop_at": None}}
        known = dict(none, sources=[{"name": "192.0.2.1"}])
        with tempfile.TemporaryDirectory() as d, mock.patch.object(heal, "chronyc", return_value=True) as cc, \
             mock.patch.object(heal, "has_default_route") as route, mock.patch.object(heal.status, "chrony") as chrony, \
             mock.patch.object(heal, "log"), mock.patch.object(heal, "write_status"):
            wd = heal.Watchdog(os.path.join(d, "config.json"))
            for has_route, c, asked in ((False, none, False), (True, none, True), (True, known, False)):
                cc.reset_mock()
                route.return_value, chrony.return_value = has_route, c
                wd.look(1000.0)
                self.assertEqual(mock.call("refresh") in cc.call_args_list, asked, (has_route, c["sources"]))

    def test_link_says_why_it_is_down(self):
        link = heal.Link()
        link.step(False, ["1.1.1.1"], lambda s: True)
        self.assertIn("no default route", link.reason)
        link.step(True, ["1.1.1.1"], lambda s: False)
        link.step(True, ["2.2.2.2"], lambda s: False)
        self.assertIn("last tried 2.2.2.2", link.reason)

    def test_default_route_parse(self):
        hdr = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
        with tempfile.NamedTemporaryFile("w") as f:
            f.write(hdr + "enp4s0\t00000000\tFE01010A\t0003\t0\t0\t1003\t00000000\n"); f.flush()
            self.assertTrue(heal.has_default_route(f.name))
        with tempfile.NamedTemporaryFile("w") as f:
            f.write(hdr + "enp4s0\t0001010A\t00000000\t0001\t0\t0\t1003\t00FFFFFF\n"); f.flush()
            self.assertFalse(heal.has_default_route(f.name))
        self.assertFalse(heal.has_default_route("/nonexistent"))
