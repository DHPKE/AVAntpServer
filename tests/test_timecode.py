import copy
import datetime
import json
import os
import socket
import tempfile
import time
import unittest

from avantp import config, timecode as tcm
from tests.test_avantp import BASE


def utc(*a):
    return datetime.datetime(*a, tzinfo=datetime.timezone.utc).timestamp()


class TimeMath(unittest.TestCase):
    def test_time_of_day_per_zone_including_daylight_saving(self):
        e = utc(2026, 10, 23, 12, 0, 0)                                        # Berlin and New York are on summer time
        self.assertEqual(tcm.timecode_at(e, "UTC", 25), (12, 0, 0, 0))
        self.assertEqual(tcm.timecode_at(e, "Europe/Berlin", 25), (14, 0, 0, 0))
        self.assertEqual(tcm.timecode_at(e, "America/New_York", 25), (8, 0, 0, 0))
        self.assertEqual(tcm.timecode_at(e, "Asia/Kathmandu", 25), (17, 45, 0, 0))   # +05:45
        w = utc(2026, 12, 23, 12, 0, 0)                                        # winter: Berlin +1, New York -5
        self.assertEqual(tcm.timecode_at(w, "Europe/Berlin", 25)[0], 13)
        self.assertEqual(tcm.timecode_at(w, "America/New_York", 25)[0], 7)

    def test_the_dst_changeover_moves_the_clock_by_an_hour(self):
        before = utc(2026, 10, 25, 0, 59, 59)                                  # 02:59:59 CEST
        after = utc(2026, 10, 25, 1, 0, 0)                                     # 02:00:00 CET again
        self.assertEqual(tcm.timecode_at(before, "Europe/Berlin", 25)[:3], (2, 59, 59))
        self.assertEqual(tcm.timecode_at(after, "Europe/Berlin", 25)[:3], (2, 0, 0))

    def test_frames_follow_the_frame_rate(self):
        base = utc(2026, 10, 23, 12, 0, 0)
        for fps, rate in ((24, 24), (25, 25), (30, 30)):
            self.assertEqual(tcm.timecode_at(base + 0.5, "UTC", fps)[3], rate // 2, fps)       # half a second in
            self.assertEqual(tcm.timecode_at(base + 0.999, "UTC", fps)[3], rate - 1, fps)      # never reaches `rate`
        self.assertEqual(tcm.timecode_at(base, "UTC", 29.97)[:3], (12, 0, 0))

    def test_drop_frame_labels(self):
        self.assertEqual(tcm.drop_frame(0), (0, 0, 0, 0))
        self.assertEqual(tcm.drop_frame(1798), (0, 0, 59, 28))
        self.assertEqual(tcm.drop_frame(1799), (0, 0, 59, 29))
        self.assertEqual(tcm.drop_frame(1800), (0, 1, 0, 2))                  # frames 0 and 1 are skipped
        self.assertEqual(tcm.drop_frame(17982), (0, 10, 0, 0))                # but not every tenth minute
        self.assertEqual(tcm.drop_frame(17982 * 6)[:2], (1, 0))               # an hour of 29.97 fps is 107892 frames
        self.assertEqual(tcm.drop_frame(17982 * 6 * 24 - 1)[0], 23)

    def test_every_frame_of_a_second_is_labelled_exactly_once_at_every_rate(self):
        base = utc(2026, 10, 23, 12, 0, 0)
        for fps in (24, 25, 30, 29.97):
            rate = tcm.FRAME_RATE[fps]
            labels = [tcm.timecode_at(base + (k + 0.0) / rate + 1e-4, "UTC", fps) for k in range(int(rate * 3))]
            self.assertEqual(len(set(labels)), len(labels), f"{fps} fps: a frame label repeated")
            if fps == 29.97:      # drop-frame counts 29.97 frames/s and so drifts up to a couple of frames from the wall clock
                self.assertEqual(labels[0][:3], (12, 0, 0))
                self.assertLessEqual(labels[0][3], 2)
            else:
                self.assertEqual(labels[0], (12, 0, 0, 0))

    def test_artnet_packet_layout(self):
        p = tcm.artnet_packet(13, 5, 21, 7, 25)
        self.assertEqual(len(p), 19)
        self.assertEqual(p[:8], b"Art-Net\x00")
        self.assertEqual(p[8:10], bytes([0x00, 0x97]))                        # OpCode 0x9700, low byte first
        self.assertEqual(p[10:12], bytes([0, 14]))                            # protocol version 14
        self.assertEqual(p[14:19], bytes([7, 21, 5, 13, 1]))                  # frames, seconds, minutes, hours, type 1 = 25 fps
        for fps, typ in ((24, 0), (25, 1), (29.97, 2), (30, 3)):
            self.assertEqual(tcm.artnet_packet(0, 0, 0, 0, fps)[18], typ)


class SupervisionTest(unittest.TestCase):
    def test_the_sender_loop_survives_an_unexpected_error_and_logs_it(self):
        from unittest import mock
        d = tempfile.mkdtemp()
        s = tcm.Sender(os.path.join(d, "c.json"), os.path.join(d, "s.json"))
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("bug in the sender")
            if calls["n"] == 3:
                return None
            raise SystemExit                       # ends the test: SystemExit is not an Exception, so the loop lets it out
        s.tick = flaky
        with mock.patch("avantp.timecode.time.sleep") as sl, self.assertLogs("timecode", "ERROR") as cm:
            with self.assertRaises(SystemExit):
                s.run()
        self.assertEqual(calls["n"], 4)                                    # it kept going after both failures
        self.assertTrue(any("unexpected error in the sender loop (failure 2 in a row)" in m for m in cm.output))
        self.assertTrue(any("Traceback" in m and "bug in the sender" in m for m in cm.output))          # with the traceback
        self.assertIn(1, [c.args[0] for c in sl.call_args_list])           # backs off, so a permanent fault cannot spin
        self.assertIn(2, [c.args[0] for c in sl.call_args_list])

    def test_state_changes_are_logged_once_not_every_frame(self):
        d = tempfile.mkdtemp()
        cfg = copy.deepcopy(BASE)
        for v in cfg["vlans"]:
            v["timecode"] = {"artnet": False, "mtc_rtp": False, "fps": 25}
        cfg["vlans"][1]["timecode"]["artnet"] = True
        json.dump(config.dump(config.validate(cfg)), open(os.path.join(d, "c.json"), "w"))
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", 0))
        s = tcm.Sender(os.path.join(d, "c.json"), os.path.join(d, "s.json"), dest_override=("127.0.0.1", "127.0.0.1", rx.getsockname()[1]))
        with self.assertLogs("timecode", "INFO") as cm:
            end = time.time() + 0.6
            while time.time() < end:
                s.tick()
                time.sleep(0.004)
        rx.close()
        sending = [m for m in cm.output if "Art-Net sending" in m]
        self.assertEqual(len(sending), 1, cm.output)                       # ~15 frames were sent, one log line
        self.assertIn("VLAN 20 show", sending[0])
        self.assertTrue(any("configuration loaded" in m for m in cm.output))


class AddressTest(unittest.TestCase):
    def test_broadcast_is_derived_when_the_kernel_has_none(self):
        from unittest import mock
        js = json.dumps([
            {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8, "broadcast": "127.255.255.255"}]},
            {"ifname": "enp3s0.20", "addr_info": [{"family": "inet", "local": "10.20.0.2", "prefixlen": 24}]},
            {"ifname": "enp3s0.30", "addr_info": [{"family": "inet", "local": "10.3.10.1", "prefixlen": 19, "broadcast": "10.3.31.255"}]},
            {"ifname": "ppp0", "addr_info": [{"family": "inet", "local": "10.0.0.1", "prefixlen": 32}]},
            {"ifname": "x", "addr_info": [{"family": "inet6", "local": "fe80::1", "prefixlen": 64}]}])
        with mock.patch("avantp.timecode.subprocess.run", return_value=mock.Mock(stdout=js)):
            a = tcm.vlan_addresses()
        self.assertEqual(a["enp3s0.20"], ("10.20.0.2", "10.20.0.255", "10.20.0.0/24"))   # broadcast derived
        self.assertEqual(a["enp3s0.30"], ("10.3.10.1", "10.3.31.255", "10.3.0.0/19"))    # as reported; network from the prefix
        self.assertNotIn("ppp0", a)                                                       # a /32 has no broadcast
        self.assertNotIn("x", a)
        with mock.patch("avantp.timecode.subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(tcm.vlan_addresses(), {})


class UnicastTest(unittest.TestCase):
    """Art-Net to one receiver instead of a broadcast."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = os.path.join(self.dir, "c.json")
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.bind(("127.0.0.1", 0))
        self.rx.settimeout(0.05)
        self.sent = []

    def tearDown(self):
        self.rx.close()

    def write(self, dest, tz="UTC"):
        c = copy.deepcopy(BASE)
        for v in c["vlans"]:
            v["timecode"] = {"artnet": False, "mtc_rtp": False, "fps": 25}
        c["vlans"][1]["timecode"] = {"artnet": True, "mtc_rtp": False, "fps": 25, "artnet_dest": dest}
        json.dump(config.dump(config.validate(c)), open(self.cfg, "w"))

    def sender(self, addresses):
        s = tcm.Sender(self.cfg, os.path.join(self.dir, "s.json"), addresses=lambda: addresses)
        # capture what would be sent and where, without a real network
        class FakeSock:
            def sendto(inner, data, addr):
                self.sent.append((addr, data))
            def close(inner): pass
        s._socket = lambda ip: FakeSock()
        return s

    def run_for(self, s, secs=0.3):
        end = time.time() + secs
        while time.time() < end:
            s.tick()
            time.sleep(0.004)
        s.write_status(force=True)
        return [x for x in json.load(open(os.path.join(self.dir, "s.json")))["vlans"] if x["id"] == 20][0]["artnet"]

    def test_unicast_goes_to_that_address_only(self):
        self.write("10.20.0.50")
        a = self.run_for(self.sender({"enp3s0.20": ("10.20.0.2", "10.20.0.255", "10.20.0.0/24")}))
        self.assertGreater(len(self.sent), 3)
        self.assertEqual({addr for addr, _ in self.sent}, {("10.20.0.50", 6454)})        # never the broadcast address
        self.assertEqual(a["state"], "sending")
        self.assertEqual(a["dest"], "10.20.0.50:6454 (unicast)")
        self.assertEqual(self.sent[0][1][:10], b"Art-Net\x00\x00\x97")                      # still a normal ArtTimeCode packet

    def test_broadcast_is_still_the_default_and_says_so(self):
        self.write("broadcast")
        a = self.run_for(self.sender({"enp3s0.20": ("10.20.0.2", "10.20.0.255", "10.20.0.0/24")}))
        self.assertEqual({addr for addr, _ in self.sent}, {("10.20.0.255", 6454)})
        self.assertEqual(a["dest"], "10.20.0.255:6454 (broadcast)")

    def test_a_destination_off_the_vlan_is_refused_not_sent(self):
        # a DHCP VLAN: the subnet is only known at run time. 8.8.8.8 would leave through the WAN's default route
        c = copy.deepcopy(BASE)
        c["vlans"][2]["timecode"] = {"artnet": True, "mtc_rtp": False, "fps": 25, "artnet_dest": "8.8.8.8"}   # VLAN 30 is DHCP
        json.dump(config.dump(config.validate(c)), open(self.cfg, "w"))
        s = self.sender({"enp3s0.30": ("10.3.10.1", "10.3.31.255", "10.3.0.0/19")})
        end = time.time() + 0.3
        while time.time() < end:
            s.tick()
            time.sleep(0.004)
        s.write_status(force=True)
        a = [x for x in json.load(open(os.path.join(self.dir, "s.json")))["vlans"] if x["id"] == 30][0]["artnet"]
        self.assertEqual(self.sent, [])                                                 # nothing at all went out
        self.assertEqual(a["state"], "error")
        self.assertIn("not on this VLAN's network 10.3.0.0/19", a["error"])
        # ... and it recovers by itself once the receiver is on the VLAN
        c["vlans"][2]["timecode"]["artnet_dest"] = "10.3.5.5"
        json.dump(config.dump(config.validate(c)), open(self.cfg, "w"))
        os.utime(self.cfg, (time.time() + 5, time.time() + 5))
        end = time.time() + 0.3
        while time.time() < end:
            s.tick()
            time.sleep(0.004)
        self.assertEqual({addr for addr, _ in self.sent}, {("10.3.5.5", 6454)})


def src_dev(src):
    return {"10.20.0.2": "enp3s0.20", "10.3.10.1": "enp3s0.30"}.get(src)


class RouteCheckTest(UnicastTest):
    """The kernel decides the interface: an address inside the VLAN's /19 may still be routed to the WAN's /24."""

    def sender2(self, addresses, route, now=time.time):
        s = self.sender(addresses)
        s.route, s.now = route, now
        return s

    def test_an_address_the_kernel_routes_to_the_wan_is_refused_with_a_clear_reason(self):
        c = copy.deepcopy(BASE)
        c["wan"] = {"iface": "enp4s0", "mode": "dhcp"}
        c["vlans"][2]["timecode"] = {"artnet": True, "mtc_rtp": False, "fps": 25, "artnet_dest": "10.3.1.50"}   # VLAN 30, DHCP
        json.dump(config.dump(config.validate(c)), open(self.cfg, "w"))
        s = self.sender2({"enp3s0.30": ("10.3.10.1", "10.3.31.255", "10.3.0.0/19")}, lambda dest, src: "enp4s0")
        end = time.time() + 0.3
        while time.time() < end:
            s.tick()
            time.sleep(0.004)
        s.write_status(force=True)
        a = [x for x in json.load(open(os.path.join(self.dir, "s.json")))["vlans"] if x["id"] == 30][0]["artnet"]
        self.assertEqual(self.sent, [])                                                 # nothing left the sender
        self.assertEqual(a["state"], "error")
        self.assertIn("on the WAN side (enp4s0)", a["error"])
        self.assertIn("Choose a receiver on this VLAN (10.3.0.0/19)", a["error"])

    def test_an_already_active_config_with_a_bad_receiver_still_runs_the_other_vlans(self):
        """Regression: a stricter save-time rule must not turn an active configuration into 'no usable configuration'."""
        c = copy.deepcopy(BASE)
        c["wan"] = {"iface": "enp4s0", "mode": "static", "address": "10.3.1.24/24", "gateway": "10.3.1.254"}      # inside VLAN 30's /19
        c["vlans"][1]["timecode"] = {"artnet": True, "mtc_rtp": False, "fps": 25, "artnet_dest": "broadcast"}          # VLAN 20 fine
        c["vlans"][2]["timecode"] = {"artnet": True, "mtc_rtp": False, "fps": 25, "artnet_dest": "10.3.5.5"}             # VLAN 30 fine
        good = config.dump(config.validate(c))
        good["vlans"][2]["timecode"]["artnet_dest"] = "10.3.1.50"                                                        # now on the WAN network
        with self.assertRaises(config.ConfigError):
            config.validate(copy.deepcopy(good))                                    # saving it is refused ...
        json.dump(good, open(self.cfg, "w"))                                        # ... but it is already the active config
        s = self.sender2({"enp3s0.20": ("10.20.0.2", "10.20.0.255", "10.20.0.0/24"),
                          "enp3s0.30": ("10.3.10.1", "10.3.31.255", "10.3.0.0/19")}, lambda d, src: "enp4s0" if d.startswith("10.3.1.") else src_dev(src))
        end = time.time() + 0.4
        while time.time() < end:
            s.tick()
            time.sleep(0.004)
        s.write_status(force=True)
        st = {v["id"]: v["artnet"] for v in json.load(open(os.path.join(self.dir, "s.json")))["vlans"]}
        self.assertEqual(st[20]["state"], "sending")                                  # the other VLAN keeps its timecode
        self.assertEqual(st[30]["state"], "error")                                    # only the bad receiver is refused
        self.assertIn("on the WAN side", st[30]["error"])
        self.assertEqual({a for a, _ in self.sent}, {("10.20.0.255", 6454)})

    def test_routing_through_the_vlan_is_fine_and_an_unknown_route_does_not_block(self):
        self.write("10.20.0.50")
        self.run_for(self.sender2({"enp3s0.20": ("10.20.0.2", "10.20.0.255", "10.20.0.0/24")}, lambda d, s: "enp3s0.20"))
        self.assertGreater(len(self.sent), 3)
        self.sent.clear()
        self.run_for(self.sender2({"enp3s0.20": ("10.20.0.2", "10.20.0.255", "10.20.0.0/24")}, lambda d, s: None))   # `ip` failed
        self.assertGreater(len(self.sent), 3)                                           # the firewall remains the backstop

    def test_a_route_that_changes_later_is_noticed(self):
        self.write("10.20.0.50")
        clock = [time.time()]
        via = ["enp3s0.20"]
        s = self.sender2({"enp3s0.20": ("10.20.0.2", "10.20.0.255", "10.20.0.0/24")}, lambda d, src: via[0], now=lambda: clock[0])
        for _ in range(3):
            clock[0] += 0.05
            s.tick()
        self.assertGreater(len(self.sent), 0)
        self.sent.clear()
        via[0] = "enp4s0"                                                               # a more specific route appears
        clock[0] += 6                                                                   # the check repeats every 5 s
        s.tick()
        clock[0] += 0.2
        s.tick()
        self.assertEqual(self.sent, [])

    def test_a_blocked_send_is_explained_not_just_errno_1(self):
        self.write("10.20.0.50")
        s = self.sender2({"enp3s0.20": ("10.20.0.2", "10.20.0.255", "10.20.0.0/24")}, lambda d, src: None)

        class Blocked:
            def sendto(inner, data, addr):
                raise PermissionError(1, "Operation not permitted")
            def close(inner): pass
        s._socket = lambda ip: Blocked()
        a = self.run_for(s, 0.2)
        self.assertEqual(a["state"], "error")
        self.assertIn("the firewall blocked the packet", a["error"])
        self.assertNotIn("Operation not permitted", a["error"])


class SenderTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg_path = os.path.join(self.dir, "config.json")
        self.status = os.path.join(self.dir, "status.json")
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.bind(("127.0.0.1", 0))
        self.rx.settimeout(0.05)
        self.port = self.rx.getsockname()[1]

    def tearDown(self):
        self.rx.close()

    def write_cfg(self, mutate=None):
        c = copy.deepcopy(BASE)
        for v in c["vlans"]:
            v["timecode"] = {"artnet": False, "mtc_rtp": False, "fps": 25}
        if mutate:
            mutate(c)
        json.dump(config.dump(config.validate(c)), open(self.cfg_path, "w"))

    def packets(self, secs):
        out, end = [], time.time() + secs
        while time.time() < end:
            yield_ = None
            try:
                yield_ = self.rx.recv(100)
            except socket.timeout:
                pass
            if yield_:
                out.append(yield_)
        return out

    def run_sender(self, s, secs):
        got, end = [], time.time() + secs
        while time.time() < end:
            s.tick()
            try:
                while True:
                    got.append(self.rx.recv(100))
            except socket.timeout:
                pass
            time.sleep(0.005)
        return got

    def test_nothing_is_sent_while_every_toggle_is_off(self):
        self.write_cfg()
        s = tcm.Sender(self.cfg_path, self.status, dest_override=("127.0.0.1", "127.0.0.1", self.port))
        self.assertEqual(self.run_sender(s, 0.5), [])
        st = json.load(open(self.status))
        self.assertTrue(all(v["artnet"]["state"] == "off" and v["mtc_rtp"]["state"] == "off" for v in st["vlans"]))

    def test_one_packet_per_frame_in_the_vlans_own_time_zone(self):
        def mutate(c):
            c["vlans"][1]["timecode"] = {"artnet": True, "mtc_rtp": False, "fps": 25}        # VLAN 20, New York
        self.write_cfg(mutate)
        s = tcm.Sender(self.cfg_path, self.status, dest_override=("127.0.0.1", "127.0.0.1", self.port))
        t0 = time.time()
        got = self.run_sender(s, 1.0)
        n = len(got)
        self.assertTrue(20 <= n <= 30, f"expected about 25 packets in 1 s, got {n}")
        ny = datetime.datetime.fromtimestamp(t0, __import__("zoneinfo").ZoneInfo("America/New_York"))
        first = got[0]
        self.assertEqual(first[:8], b"Art-Net\x00")
        self.assertEqual(first[17], ny.hour)                                              # hours field = New York's hour
        frames = [p[14] for p in got]
        for a, b in zip(frames, frames[1:]):
            self.assertIn((b - a) % 25, (1, 2), "frames must advance one at a time (a late tick may skip one)")
        s.write_status(force=True)                         # the service refreshes this file about once a second
        st = json.load(open(self.status))
        v = [x for x in st["vlans"] if x["id"] == 20][0]
        self.assertEqual(v["artnet"]["state"], "sending")
        self.assertEqual(v["artnet"]["fps"], 25)
        self.assertEqual(v["artnet"]["sent"], n)
        self.assertEqual(v["artnet"]["dest"], f"127.0.0.1:{self.port}")
        self.assertRegex(v["artnet"]["last"], r"^\d\d:\d\d:\d\d:\d\d$")
        self.assertEqual([x["artnet"]["state"] for x in st["vlans"] if x["id"] != 20], ["off", "off"])

    def test_waits_for_an_address_on_dhcp_vlans(self):
        self.write_cfg(lambda c: c["vlans"][1]["timecode"].update(artnet=True))
        s = tcm.Sender(self.cfg_path, self.status, addresses=lambda: {})
        s.tick()
        s.write_status(force=True)
        v = [x for x in json.load(open(self.status))["vlans"] if x["id"] == 20][0]
        self.assertEqual(v["artnet"]["state"], "no-address")
        s.addresses = lambda: {"enp3s0.20": ("127.0.0.1", "127.0.0.1")}                    # the lease arrives
        s.addr_at = 0
        s.dest_override = None
        orig = tcm.ARTNET_PORT
        tcm.ARTNET_PORT = self.port
        try:
            self.run_sender(s, 0.3)
        finally:
            tcm.ARTNET_PORT = orig
        s.write_status(force=True)
        v = [x for x in json.load(open(self.status))["vlans"] if x["id"] == 20][0]
        self.assertEqual(v["artnet"]["state"], "sending")

    def test_a_changed_config_is_picked_up_without_restart(self):
        self.write_cfg()
        s = tcm.Sender(self.cfg_path, self.status, dest_override=("127.0.0.1", "127.0.0.1", self.port))
        self.assertEqual(self.run_sender(s, 0.2), [])
        time.sleep(0.05)
        self.write_cfg(lambda c: c["vlans"][1]["timecode"].update(artnet=True, fps=30))
        os.utime(self.cfg_path, (time.time() + 5, time.time() + 5))
        got = self.run_sender(s, 0.6)
        self.assertGreater(len(got), 10)
        self.assertEqual(got[0][18], 3)                                                    # now 30 fps
        self.write_cfg()                                                                   # switched off again
        os.utime(self.cfg_path, (time.time() + 10, time.time() + 10))
        self.run_sender(s, 0.1)
        self.assertEqual(self.run_sender(s, 0.3), [])

    def test_a_broken_config_keeps_the_last_good_one(self):
        self.write_cfg(lambda c: c["vlans"][1]["timecode"].update(artnet=True))
        s = tcm.Sender(self.cfg_path, self.status, dest_override=("127.0.0.1", "127.0.0.1", self.port))
        self.assertGreater(len(self.run_sender(s, 0.3)), 3)
        open(self.cfg_path, "w").write("{ not json")
        os.utime(self.cfg_path, (time.time() + 5, time.time() + 5))
        self.assertGreater(len(self.run_sender(s, 0.3)), 3)


if __name__ == "__main__":
    unittest.main()
