import copy
import datetime
import json
import os
import socket
import struct
import tempfile
import time
import unittest
import zoneinfo

from avantp import config, rtpmidi as r, timecode as tcm
from tests.test_avantp import BASE


def free_pair():
    """Two consecutive free UDP ports (control, data)."""
    for _ in range(200):
        a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        a.bind(("127.0.0.1", 0))
        c = a.getsockname()[1]
        b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            b.bind(("127.0.0.1", c + 1))
            a.close()
            b.close()
            return c
        except OSError:
            a.close()
            b.close()
    raise RuntimeError("no free port pair")


class Peer:
    """A stand-in for a Mac / rtpMIDI participant: it INVITES the box (it is the session initiator)."""

    def __init__(self, port, token=0x1234, name="Test Mac"):
        self.port, self.token, self.name, self.ssrc = port, token, name, 0xCAFEBABE
        self.ctrl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ctrl.bind(("127.0.0.1", 0))
        self.data = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.data.bind(("127.0.0.1", self.ctrl.getsockname()[1] + 1)) if False else self.data.bind(("127.0.0.1", 0))
        for s in (self.ctrl, self.data):
            s.settimeout(0.05)

    def send(self, sock, data, to):
        sock.sendto(data, ("127.0.0.1", to))

    def expect(self, sock, ep, secs=1.0):
        """Run the endpoint while waiting for one datagram from it."""
        end = time.time() + secs
        while time.time() < end:
            ep.poll()
            try:
                return sock.recvfrom(1500)[0]
            except socket.timeout:
                pass
        return None

    def join(self, ep):
        self.send(self.ctrl, r.build_inv("IN", self.token, self.ssrc, self.name.encode()), self.port)
        c = self.expect(self.ctrl, ep)
        self.send(self.data, r.build_inv("IN", self.token, self.ssrc, self.name.encode()), self.port + 1)
        d = self.expect(self.data, ep)
        return c, d

    def clock_sync(self, ep):
        """Send CK0 and return the CK reply, skipping the RTP packets (the full-frame welcome) that may arrive first."""
        self.send(self.data, r.build_ck(self.ssrc, 0, (12345, 0, 0)), self.port + 1)
        end = time.time() + 1.0
        while time.time() < end:
            pkt = self.expect(self.data, ep, 0.2)
            if pkt is not None and r.parse(pkt) and r.parse(pkt)["cmd"] == "CK":
                return pkt
        return None

    def close(self):
        self.ctrl.close()
        self.data.close()


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.port = free_pair()
        self.ep = r.Endpoint("AVAntp Test VLAN", "127.0.0.1", 25, "UTC", None, (self.port, self.port + 1))
        self.peer = Peer(self.port)

    def tearDown(self):
        self.ep.close()
        self.peer.close()

    def test_handshake_and_clock_sync(self):
        c, d = self.peer.join(self.ep)
        for reply in (c, d):
            m = r.parse(reply)
            self.assertEqual((m["cmd"], m["version"], m["token"], m["ssrc"]), ("OK", 2, 0x1234, self.ep.ssrc))
            self.assertEqual(m["name"], "AVAntp Test VLAN")
        self.assertEqual(len(self.ep.connected()), 1)
        ck = r.parse(self.peer.clock_sync(self.ep))
        self.assertEqual((ck["cmd"], ck["count"], ck["ssrc"]), ("CK", 1, self.ep.ssrc))
        self.assertEqual(ck["ts"][0], 12345)                                    # their timestamp echoed
        self.assertGreaterEqual(ck["ts"][1], 0)                                 # our time in 100 us units
        st = self.ep.status()
        self.assertEqual((st["state"], st["listen"], len(st["peers"])), ("sending", f"127.0.0.1:{self.port}", 1))
        self.assertEqual(st["peers"][0]["name"], "Test Mac")

    def test_listening_state_before_anyone_connects(self):
        self.assertEqual(self.ep.status()["state"], "listening")

    def test_a_joining_receiver_gets_a_full_frame_message_first(self):
        self.peer.join(self.ep)
        pkt = None
        end = time.time() + 1
        while time.time() < end and pkt is None:
            self.ep.poll()
            try:
                pkt = self.peer.data.recv(1500)
            except socket.timeout:
                pass
        # RTP header: V=2, payload type 97 without marker bit, then the command list: length 10, MTC full frame SysEx
        self.assertEqual(pkt[0], 0x80)
        self.assertEqual(pkt[1], 0x61)                                          # payload type 97, marker clear: byte-for-byte what a Mac sends
        self.assertEqual(struct.unpack("!I", pkt[8:12])[0], self.ep.ssrc)
        self.assertEqual(pkt[12], 10)
        body = pkt[13:]
        self.assertEqual(body[:5], bytes([0xF0, 0x7F, 0x7F, 0x01, 0x01]))
        self.assertEqual(body[-1], 0xF7)
        self.assertEqual(body[5] >> 5, 1)                                       # rate code 1 = 25 fps
        now = datetime.datetime.now(datetime.timezone.utc)
        self.assertLessEqual(abs((body[5] & 31) - now.hour), 1)                 # roughly the right hour (UTC)

    def stream(self, secs=1.3):
        """Join, then collect the RTP packets for `secs` while running the endpoint's clock."""
        self.peer.join(self.ep)
        pkts, end = [], time.time() + secs
        while time.time() < end:
            self.ep.tick()
            try:
                while True:
                    pkts.append((time.time(), self.peer.data.recv(1500)))
            except socket.timeout:
                pass
            time.sleep(0.003)
        return pkts

    def test_quarter_frame_stream_is_valid_mtc(self):
        pkts = self.stream(1.6)
        qf = [(t, p) for t, p in pkts if len(p) == 15 and p[12] == 2 and p[13] == 0xF1]
        self.assertTrue(110 <= len(qf) <= 175, f"25 fps = 100 quarter frames per second, got {len(qf)} in 1.6 s")
        seqs = [struct.unpack("!H", p[2:4])[0] for _, p in pkts]
        self.assertTrue(all((b - a) & 0xFFFF == 1 for a, b in zip(seqs, seqs[1:])), "RTP sequence numbers must count up by one")
        stamps = [struct.unpack("!I", p[4:8])[0] for _, p in pkts]
        self.assertTrue(all(b >= a for a, b in zip(stamps, stamps[1:])))
        self.assertAlmostEqual((stamps[-1] - stamps[0]) / 10000.0, pkts[-1][0] - pkts[0][0], delta=0.25)    # 10 kHz clock
        pieces = [p[14] >> 4 for _, p in qf]
        self.assertTrue(all((b - a) % 8 == 1 for a, b in zip(pieces, pieces[1:])), "pieces must cycle 0..7 without gaps")
        # every full cycle starting at piece 0 decodes to a timecode that is two frames behind the wall clock at its end
        start = pieces.index(0)
        checked = 0
        for i in range(start, len(qf) - 8, 8):
            (h, m, s, f), code = r.decode_quarter_frames([p[14] for _, p in qf[i:i + 8]])
            self.assertEqual(code, 1)                                           # 25 fps
            self.assertTrue(h < 24 and m < 60 and s < 60 and f < 25)
            wall = datetime.datetime.fromtimestamp(qf[i + 7][0], zoneinfo.ZoneInfo("UTC"))
            tod = wall.hour * 3600 + wall.minute * 60 + wall.second + wall.microsecond / 1e6
            sent = h * 3600 + m * 60 + s + f / 25.0
            # the MTC rule: the value in the 8 pieces is the time at piece 0; the receiver adds two frames
            self.assertAlmostEqual(sent + 2 / 25.0, tod, delta=0.2)
            checked += 1
        self.assertGreaterEqual(checked, 8)

    def test_sequences_start_on_even_frames_so_two_frame_cycles_chain_up(self):
        pkts = self.stream(1.2)
        qf = [p for _, p in pkts if len(p) == 15 and p[13] == 0xF1]
        first = [p[14] >> 4 for p in qf].index(0)
        decoded = []
        for i in range(first, len(qf) - 8, 8):
            (h, m, s, f), _ = r.decode_quarter_frames([p[14] for p in qf[i:i + 8]])
            decoded.append(h * 3600 * 25 + m * 60 * 25 + s * 25 + f)
        self.assertGreater(len(decoded), 4)
        steps = {(b - a) % (86400 * 25) for a, b in zip(decoded, decoded[1:])}
        self.assertEqual(steps, {2})                                            # each sequence is exactly two frames after the last

    def test_29_97_drop_frame_uses_rate_code_2(self):
        self.ep.close()
        self.ep = r.Endpoint("df", "127.0.0.1", 29.97, "Europe/Vienna", None, (self.port, self.port + 1))
        pkts = self.stream(0.9)
        qf = [p for _, p in pkts if len(p) == 15 and p[13] == 0xF1]
        self.assertGreater(len(qf), 80)
        first = [p[14] >> 4 for p in qf].index(0)
        (h, m, s, f), code = r.decode_quarter_frames([p[14] for p in qf[first:first + 8]])
        self.assertEqual(code, 2)
        self.assertTrue(f < 30)

    def test_bye_ends_the_session_and_stops_the_stream(self):
        self.peer.join(self.ep)
        self.peer.send(self.peer.ctrl, r.build_inv("BY", self.peer.token, self.peer.ssrc), self.port)
        time.sleep(0.05)
        self.ep.poll()
        self.assertEqual(self.ep.connected(), [])
        self.assertEqual(self.ep.status()["state"], "listening")

    def test_silent_peers_time_out(self):
        clock = [1000.0]
        self.ep.close()
        self.ep = r.Endpoint("t", "127.0.0.1", 25, "UTC", None, (self.port, self.port + 1), now=lambda: clock[0])
        self.peer.join(self.ep)
        self.assertEqual(len(self.ep.connected()), 1)
        clock[0] += r.PEER_TIMEOUT - 5
        self.ep.poll()
        self.assertEqual(len(self.ep.connected()), 1)
        clock[0] += 10
        self.ep.poll()
        self.assertEqual(self.ep.connected(), [])


class RestartTest(unittest.TestCase):
    """After the service restarts a receiver still believes it is connected: it must be told, and recognise the box."""

    def test_the_session_identity_survives_a_restart(self):
        port = free_pair()
        a = r.Endpoint("AVAntp X", "127.0.0.1", 25, "UTC", None, (port, port + 1))
        ssrc_a = a.ssrc
        a.close()
        b = r.Endpoint("AVAntp X", "127.0.0.1", 25, "UTC", None, (port, port + 1))
        self.addCleanup(b.close)
        self.assertEqual(b.ssrc, ssrc_a)
        c = r.Endpoint("AVAntp Y", "127.0.0.1", 25, "UTC", None, (port + 10, port + 11))
        self.addCleanup(c.close)
        self.assertNotEqual(c.ssrc, ssrc_a)                                      # another VLAN/session has its own identity

    def test_a_receiver_that_keeps_syncing_with_a_forgotten_session_is_told_bye_with_our_ssrc(self):
        port = free_pair()
        ep = r.Endpoint("AVAntp X", "127.0.0.1", 25, "UTC", None, (port, port + 1))
        self.addCleanup(ep.close)
        peer = Peer(port)
        self.addCleanup(peer.close)
        peer.send(peer.data, r.build_ck(0xABCDEF01, 0, (1, 0, 0)), port + 1)         # never joined: as after a restart
        bye = r.parse(peer.expect(peer.data, ep))
        self.assertEqual((bye["cmd"], bye["ssrc"]), ("BY", ep.ssrc))
        peer.send(peer.data, r.build_ck(0xABCDEF01, 0, (2, 0, 0)), port + 1)         # again at once: rate limited
        self.assertIsNone(peer.expect(peer.data, ep, 0.2))
        self.assertEqual(ep.connected(), [])

    def test_a_receiver_that_is_connected_is_not_told_bye(self):
        port = free_pair()
        ep = r.Endpoint("AVAntp X", "127.0.0.1", 25, "UTC", None, (port, port + 1))
        self.addCleanup(ep.close)
        peer = Peer(port)
        self.addCleanup(peer.close)
        peer.join(ep)
        ck = r.parse(peer.clock_sync(ep))
        self.assertEqual(ck["cmd"], "CK")

    def test_hosts_outside_the_vlan_are_not_answered_at_all(self):
        port = free_pair()
        ep = r.Endpoint("AVAntp X", "127.0.0.1", 25, "UTC", "10.20.0.0/24", (port, port + 1))
        self.addCleanup(ep.close)
        peer = Peer(port)
        self.addCleanup(peer.close)
        peer.send(peer.data, r.build_ck(1, 0, (1, 0, 0)), port + 1)
        self.assertIsNone(peer.expect(peer.data, ep, 0.2))


class AbuseTest(unittest.TestCase):
    def setUp(self):
        self.port = free_pair()

    def endpoint(self, **kw):
        ep = r.Endpoint("AVAntp", "127.0.0.1", 25, "UTC", kw.get("net"), (self.port, self.port + 1))
        self.addCleanup(ep.close)
        return ep

    def test_garbage_is_ignored_without_reply_or_crash(self):
        ep = self.endpoint()
        peer = Peer(self.port)
        self.addCleanup(peer.close)
        junk = [b"", b"\xff", b"\xff\xff", b"\xff\xffIN", b"\xff\xffIN" + b"\0" * 5, b"\xff\xffXX" + b"\0" * 30, b"\xff\xffCK\0\0",
                b"hello", os.urandom(40), b"\x80\xe1" + b"\0" * 20, b"\xff\xffIN" + struct.pack("!II", 2, 1)]
        for j in junk:
            peer.send(peer.ctrl, j, self.port)
            peer.send(peer.data, j, self.port + 1)
        replies = [peer.expect(peer.ctrl, ep, 0.1), peer.expect(peer.data, ep, 0.1)]
        for rep in replies:
            if rep is not None:
                self.assertEqual(r.parse(rep)["cmd"], "OK")                     # only the one well-formed (if silly) invitation
        self.assertLessEqual(len(ep.peers), 1)
        self.assertEqual(ep.connected(), [])

    def test_a_long_session_name_is_cut(self):
        ep = self.endpoint()
        peer = Peer(self.port)
        self.addCleanup(peer.close)
        peer.send(peer.ctrl, r.build_inv("IN", 7, 9, b"N" * 900), self.port)
        peer.expect(peer.ctrl, ep)
        self.assertLessEqual(len(list(ep.peers.values())[0].name), 63)

    def test_a_host_outside_the_vlan_is_refused(self):
        ep = self.endpoint(net="10.20.0.0/24")                                  # the test peer connects from 127.0.0.1
        peer = Peer(self.port)
        self.addCleanup(peer.close)
        c, d = peer.join(ep)
        self.assertEqual(r.parse(c)["cmd"], "NO")
        self.assertEqual(ep.peers, {})

    def test_a_data_invitation_without_a_control_invitation_is_refused(self):
        ep = self.endpoint()
        peer = Peer(self.port)
        self.addCleanup(peer.close)
        peer.send(peer.data, r.build_inv("IN", 99, 5, b"sneaky"), self.port + 1)
        self.assertEqual(r.parse(peer.expect(peer.data, ep))["cmd"], "NO")
        self.assertEqual(ep.connected(), [])

    def test_at_most_eight_receivers(self):
        ep = self.endpoint()
        peers = [Peer(self.port, token=100 + i) for i in range(10)]
        for p in peers:
            self.addCleanup(p.close)
        answers = [r.parse(p.join(ep)[0])["cmd"] for p in peers]
        self.assertEqual(answers.count("OK"), r.MAX_PEERS)
        self.assertEqual(answers.count("NO"), 2)

    def test_a_wrong_protocol_version_is_refused(self):
        ep = self.endpoint()
        peer = Peer(self.port)
        self.addCleanup(peer.close)
        pkt = bytearray(r.build_inv("IN", 5, 6, b"old"))
        pkt[4:8] = struct.pack("!I", 1)
        peer.send(peer.ctrl, bytes(pkt), self.port)
        self.assertEqual(r.parse(peer.expect(peer.ctrl, ep))["cmd"], "NO")

    def test_an_invitation_flood_is_rate_limited(self):
        ep = self.endpoint()
        peer = Peer(self.port)
        self.addCleanup(peer.close)
        for i in range(40):
            peer.send(peer.ctrl, r.build_inv("IN", 1000 + i, 6, b"flood"), self.port)
        time.sleep(0.05)
        ep.poll()
        self.assertLessEqual(len(ep.peers), 20)


class SenderIntegrationTest(unittest.TestCase):
    def test_mtc_listener_per_vlan_with_status_and_a_real_session(self):
        d = tempfile.mkdtemp()
        cfg = copy.deepcopy(BASE)
        for v in cfg["vlans"]:
            v["timecode"] = {"artnet": False, "mtc_rtp": False, "fps": 25}
        cfg["vlans"][1]["timecode"] = {"artnet": False, "mtc_rtp": True, "fps": 25}                 # VLAN 20, New York
        json.dump(config.dump(config.validate(cfg)), open(os.path.join(d, "c.json"), "w"))
        port = free_pair()
        s = tcm.Sender(os.path.join(d, "c.json"), os.path.join(d, "s.json"), dest_override=("127.0.0.1", "127.0.0.1", 6454, port))
        s.tick()
        s.write_status(force=True)
        st = {v["id"]: v["mtc_rtp"] for v in json.load(open(os.path.join(d, "s.json")))["vlans"]}
        self.assertEqual(st[20]["state"], "listening")
        self.assertEqual(st[20]["listen"], f"127.0.0.1:{port}")
        self.assertEqual(st[20]["name"], "AVAntp show")
        self.assertEqual(st[10]["state"], "off")                                                   # the others are not listening
        peer = Peer(port)
        try:
            peer.join(s.mtc[20])
            end = time.time() + 0.6
            got = 0
            while time.time() < end:
                s.tick()
                try:
                    while True:
                        peer.data.recv(1500)
                        got += 1
                except socket.timeout:
                    pass
            s.write_status(force=True)
            st = {v["id"]: v["mtc_rtp"] for v in json.load(open(os.path.join(d, "s.json")))["vlans"]}
            self.assertEqual(st[20]["state"], "sending")
            self.assertEqual([p["name"] for p in st[20]["peers"]], ["Test Mac"])
            self.assertGreater(got, 40)
            self.assertRegex(st[20]["last"], r"^\d\d:\d\d:\d\d:\d\d$")
        finally:
            peer.close()
            for ep in s.mtc.values():
                ep.close()

    def test_a_port_already_in_use_is_reported_not_fatal(self):
        d = tempfile.mkdtemp()
        cfg = copy.deepcopy(BASE)
        for v in cfg["vlans"]:
            v["timecode"] = {"artnet": False, "mtc_rtp": False, "fps": 25}
        cfg["vlans"][1]["timecode"]["mtc_rtp"] = True
        json.dump(config.dump(config.validate(cfg)), open(os.path.join(d, "c.json"), "w"))
        port = free_pair()
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("127.0.0.1", port))                       # exclusive: no SO_REUSEADDR on this one
        try:
            s = tcm.Sender(os.path.join(d, "c.json"), os.path.join(d, "s.json"), dest_override=("127.0.0.1", "127.0.0.1", 6454, port))
            s.tick()
            s.write_status(force=True)
            st = {v["id"]: v["mtc_rtp"] for v in json.load(open(os.path.join(d, "s.json")))["vlans"]}[20]
            self.assertIn(st["state"], ("error", "listening"))   # some systems let SO_REUSEADDR share a UDP port; both are honest
            if st["state"] == "error":
                self.assertIn("cannot listen", st["error"])
        finally:
            blocker.close()
            for ep in s.mtc.values():
                ep.close()


if __name__ == "__main__":
    unittest.main()
