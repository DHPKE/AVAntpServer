"""MIDI Timecode (MTC) over RTP-MIDI (RFC 6295) with Apple's session protocol ("AppleMIDI", as used by macOS Network MIDI
and rtpMIDI on Windows).

The box is a session PARTICIPANT that listens: on each VLAN with MTC switched on it opens two UDP ports (control 5004,
data 5005) on the VLAN's own address. A receiver connects to it (macOS: Audio MIDI Setup > MIDI Network Setup > "+" under
Directory, enter the VLAN address and port 5004; Windows rtpMIDI: Add participant by address). Once connected, the box
sends the time of day in the VLAN's own time zone as MTC quarter-frame messages (4 per frame) in RTP packets, plus a
full-frame message when a receiver joins.

Everything here is plain data in / data out so it can be tested without a network: the protocol builders and parsers
are functions, and `Endpoint` only touches the sockets it is given.
"""
import logging
import random
import select
import socket
import struct
import time

from . import timecode as tc

log = logging.getLogger("rtpmidi")

CONTROL_PORT, DATA_PORT = 5004, 5005
SIG = b"\xff\xff"
MAX_PEERS = 8
PEER_TIMEOUT = 90            # seconds without any packet from a peer (they send clock sync at least every ~60 s)
MTC_RATE_CODE = {24: 0, 25: 1, 29.97: 2, 30: 3}      # bits 1-2 of the last quarter frame / bits 5-6 of the hour byte
PAYLOAD_TYPE = 97            # dynamic type, as used by Apple (second RTP byte 0x61, marker clear)
RTP_CLOCK = 10000            # AppleMIDI timestamps count 100-microsecond units


# ---------------------------------------------------------------- AppleMIDI session protocol
def parse(data):
    """Parse one AppleMIDI control datagram. Returns a dict with 'cmd' or None for anything that is not one."""
    if len(data) < 4 or data[:2] != SIG:
        return None
    cmd = data[2:4].decode("latin1")
    try:
        if cmd in ("IN", "OK", "NO"):
            ver, token, ssrc = struct.unpack("!III", data[4:16])
            name = data[16:].split(b"\0", 1)[0][:63].decode("utf-8", "replace")
            return {"cmd": cmd, "version": ver, "token": token, "ssrc": ssrc, "name": name}
        if cmd == "BY":
            ver, token, ssrc = struct.unpack("!III", data[4:16])
            return {"cmd": cmd, "version": ver, "token": token, "ssrc": ssrc}
        if cmd == "CK":
            ssrc, count = struct.unpack("!IB", data[4:9])
            ts = struct.unpack("!QQQ", data[12:36])
            return {"cmd": cmd, "ssrc": ssrc, "count": count, "ts": ts}
        if cmd == "RS":
            ssrc, seq = struct.unpack("!II", data[4:12])
            return {"cmd": cmd, "ssrc": ssrc, "seq": seq}
    except struct.error:
        return None
    return None


def build_inv(cmd, token, ssrc, name=b""):
    """IN / OK / NO: version 2, initiator token, sender's SSRC, session name."""
    body = SIG + cmd.encode("latin1") + struct.pack("!III", 2, token, ssrc)
    return body + (name + b"\0" if cmd != "BY" else b"")


def build_ck(ssrc, count, ts):
    return SIG + b"CK" + struct.pack("!IB3x", ssrc, count) + struct.pack("!QQQ", *ts)


# ---------------------------------------------------------------- RTP-MIDI and MTC
def rtp_midi(seq, timestamp, ssrc, midi):
    """One RTP packet carrying a short MIDI command list (no journal, no delta time for the first command)."""
    assert 0 < len(midi) <= 15
    # second byte = payload type 97 with the marker bit CLEAR (0x61), exactly what a Mac sends. macOS completes the session
    # with a marker-set packet (0xE1) but then drops the MIDI data (seen with the real CoreMIDI stack).
    return struct.pack("!BBHII", 0x80, PAYLOAD_TYPE, seq & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc) + bytes([len(midi)]) + midi


def quarter_frame(piece, h, m, s, f, fps):
    """The MIDI byte pair (0xF1, data) for quarter-frame number `piece` (0-7) of the timecode h:m:s:f."""
    nibble = (f & 15, f >> 4, s & 15, s >> 4, m & 15, m >> 4, h & 15, ((h >> 4) & 1) | (MTC_RATE_CODE[fps] << 1))[piece]
    return bytes([0xF1, (piece << 4) | nibble])


def full_frame(h, m, s, f, fps):
    """The MTC full-frame SysEx (sent when a receiver joins, so it can jump to the right time at once)."""
    return bytes([0xF0, 0x7F, 0x7F, 0x01, 0x01, (MTC_RATE_CODE[fps] << 5) | h, m, s, f, 0xF7])


def decode_quarter_frames(pieces):
    """Inverse of quarter_frame(): 8 data bytes (pieces 0..7) -> ((h, m, s, f), rate code). For tests and diagnostics."""
    n = [p & 15 for p in pieces]
    return ((n[6] | ((n[7] & 1) << 4), n[4] | (n[5] << 4), n[2] | (n[3] << 4), n[0] | (n[1] << 4)), (n[7] >> 1) & 3)


# ---------------------------------------------------------------- one session endpoint (one VLAN)
class Peer:
    def __init__(self, ip, token, name, ssrc, ctrl_port, now):
        self.ip, self.token, self.name, self.ssrc = ip, token, name, ssrc
        self.ctrl_port, self.data_port, self.seen, self.joined = ctrl_port, None, now, now
        self.synced = False

    @property
    def established(self):
        return self.data_port is not None


class Endpoint:
    """Listens for sessions on `local_ip` and sends MTC to every connected peer."""

    def __init__(self, name, local_ip, fps, tz, network=None, ports=(CONTROL_PORT, DATA_PORT), now=time.time):
        self.name = name.encode("utf-8")[:60].decode("utf-8", "ignore") or "AVAntp"
        self.local_ip, self.fps, self.tz, self.net, self.now = local_ip, fps, tz, network, now
        self.ports = ports
        self.ssrc = random.getrandbits(32)
        self.peers = {}                      # (ip, token) -> Peer
        self.seq = random.getrandbits(16)
        self.t0 = now()
        self.next_q = None
        self.sent = 0
        self.last = None
        self.invites = []                    # times of recent invitations (a simple rate limit)
        self.ctrl = self._open(ports[0])
        self.data = self._open(ports[1])

    def _open(self, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.local_ip, port))
        s.setblocking(False)
        return s

    def sockets(self):
        return [self.ctrl, self.data]

    def close(self):
        for s in self.sockets():
            try:
                s.close()
            except OSError:
                pass

    def ts(self):
        return int((self.now() - self.t0) * RTP_CLOCK) & 0xFFFFFFFF

    # ---- incoming session traffic
    def poll(self):
        """Handle everything waiting on the two sockets."""
        for sock, is_data in ((self.ctrl, False), (self.data, True)):
            for _ in range(64):                                   # never let one chatty host starve the sender
                try:
                    data, addr = sock.recvfrom(1500)
                except (BlockingIOError, InterruptedError):
                    break
                except OSError:
                    break
                try:
                    self.handle(sock, is_data, data, addr)
                except Exception:
                    log.exception("ignoring a packet from %s that could not be handled", addr)
        self.expire()

    def allowed(self, ip):
        import ipaddress
        try:
            return self.net is None or ipaddress.ip_address(ip) in ipaddress.ip_network(self.net)
        except ValueError:
            return False

    def handle(self, sock, is_data, data, addr):
        msg = parse(data)
        if not msg:
            return                                                 # RTP from a peer, junk, or a probe: ignore
        ip, now = addr[0], self.now()
        if msg["cmd"] == "IN":
            self.invites = [t for t in self.invites if now - t < 1.0]
            known = (ip, msg["token"]) in self.peers
            if not self.allowed(ip) or msg["version"] != 2 or (not known and len(self.peers) >= MAX_PEERS) or len(self.invites) >= 20:
                log.warning("session invitation from %s refused (%s)", ip, "outside this VLAN" if not self.allowed(ip) else "too many peers or too fast")
                sock.sendto(build_inv("NO", msg["token"], self.ssrc, self.name.encode()), addr)
                return
            self.invites.append(now)
            if not is_data:
                self.peers[(ip, msg["token"])] = Peer(ip, msg["token"], msg["name"], msg["ssrc"], addr[1], now)
            else:
                p = self.peers.get((ip, msg["token"]))
                if not p:                                          # the data invitation must follow a control invitation
                    sock.sendto(build_inv("NO", msg["token"], self.ssrc, self.name.encode()), addr)
                    return
                p.data_port, p.seen = addr[1], now
                log.info("MTC session established with %s (%s)", ip, p.name)
            sock.sendto(build_inv("OK", msg["token"], self.ssrc, self.name.encode()), addr)
            if is_data:
                self._announce(p, now)
        elif msg["cmd"] == "BY":
            if self.peers.pop((ip, msg["token"]), None):
                log.info("MTC session with %s ended by the peer", ip)
        elif msg["cmd"] == "CK" and is_data:
            p = next((q for q in self.peers.values() if q.ip == ip and q.ssrc == msg["ssrc"]), None)
            if p:
                p.seen = now
                if msg["count"] == 0:                              # the peer starts the clock sync: answer with our time
                    sock.sendto(build_ck(self.ssrc, 1, (msg["ts"][0], self.ts() * 1, 0)), addr)
                else:
                    p.synced = True
        elif msg["cmd"] == "RS":
            p = next((q for q in self.peers.values() if q.ip == ip and q.ssrc == msg["ssrc"]), None)
            if p:
                p.seen = now

    def expire(self):
        now = self.now()
        for key in [k for k, p in self.peers.items() if now - p.seen > PEER_TIMEOUT]:
            log.info("MTC session with %s timed out", self.peers[key].ip)
            del self.peers[key]

    # ---- outgoing MTC
    def _label(self, frame_index):
        return tc.timecode_at(frame_index / tc.FRAME_RATE[self.fps] + 1e-4, self.tz, self.fps)

    def _send(self, midi, peers=None):
        pkt = rtp_midi(self.seq, self.ts(), self.ssrc, midi)
        self.seq = (self.seq + 1) & 0xFFFF
        for p in (peers if peers is not None else self.peers.values()):
            if p.established:
                try:
                    self.data.sendto(pkt, (p.ip, p.data_port))
                except OSError as e:
                    log.warning("MTC send to %s failed: %s", p.ip, e)

    def _announce(self, peer, now):
        """A receiver just joined: tell it the time at once (MTC full frame), it then follows the quarter frames."""
        h, m, s, f = self._label(int(now * tc.FRAME_RATE[self.fps]))
        self._send(full_frame(h, m, s, f, self.fps), [peer])

    def connected(self):
        return [p for p in self.peers.values() if p.established]

    def tick(self):
        """Send every quarter frame that is due. Returns the time the next one is due."""
        self.poll()
        rate = tc.FRAME_RATE[self.fps]
        step = 1.0 / (4 * rate)
        t = self.now()
        if self.next_q is None or t - self.next_q * step > 1.0:      # first time, or we fell behind: realign
            self.next_q = int(t / step) + 1
        while self.next_q * step <= t:
            q = self.next_q
            frame, k = divmod(q, 4)
            start = frame - (frame % 2)                              # a sequence starts on an even frame and covers two
            piece = (frame % 2) * 4 + k
            if self.connected():
                h, m, s, f = self._label(start)
                self._send(quarter_frame(piece, h, m, s, f, self.fps))
                self.sent += 1
                if piece == 7:
                    self.last = "%02d:%02d:%02d:%02d" % self._label(start + 2)
            self.next_q += 1
        return self.next_q * step

    def status(self):
        now = self.now()
        n = len(self.connected())
        return {"state": "sending" if n else "listening", "listen": f"{self.local_ip}:{self.ports[0]}", "name": self.name,
                "fps": self.fps, "sent": self.sent, "last": self.last,
                "peers": [{"ip": p.ip, "name": p.name, "age": round(now - p.joined)} for p in self.connected()]}
