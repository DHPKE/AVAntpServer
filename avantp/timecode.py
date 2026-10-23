"""Timecode senders. Time of day, in each VLAN's own time zone, sent at the VLAN's frame rate.

Art-Net: an ArtTimeCode packet (UDP 6454) per frame, broadcast on the VLAN's subnet, only for VLANs that have the
toggle on. The clock is the system clock (kept right by chrony). This runs unprivileged: it only needs to send UDP.
MTC over RTP-MIDI is not implemented yet; the status says so instead of pretending.

Status is written to a JSON file about once a second for the WebUI. The active config is re-read when it changes,
so an apply takes effect without restarting this service.
"""
import datetime
import ipaddress
import json
import logging
import os
import socket
import struct
import subprocess
import time
import zoneinfo

from . import config

log = logging.getLogger("timecode")
ARTNET_PORT = 6454
ARTNET_OPCODE_TIMECODE = 0x9700
# ArtTimeCode "Type": 0 = 24 fps, 1 = 25 fps, 2 = 29.97 fps (drop frame), 3 = 30 fps
ARTNET_TYPE = {24: 0, 25: 1, 29.97: 2, 30: 3}
FRAME_RATE = {24: 24.0, 25: 25.0, 29.97: 30000 / 1001, 30: 30.0}


def drop_frame(n):
    """Frame count since midnight -> (h, m, s, f) in 29.97 fps drop-frame labelling (frames 0 and 1 are skipped each
    minute except every tenth minute)."""
    d, m = divmod(n, 17982)
    n += 18 * d + (2 * ((m - 2) // 1798) if m >= 2 else 0)
    return n // 108000 % 24, n // 1800 % 60, n // 30 % 60, n % 30


def timecode_at(epoch, tz, fps):
    """Time of day at `epoch` in zone `tz` as (hours, minutes, seconds, frames) for the frame rate `fps`."""
    local = datetime.datetime.fromtimestamp(epoch, zoneinfo.ZoneInfo(tz))
    since_midnight = local.hour * 3600 + local.minute * 60 + local.second + local.microsecond / 1e6
    if fps == 29.97:
        return drop_frame(int(since_midnight * 30000 / 1001))
    rate = int(fps)
    return local.hour, local.minute, local.second, min(rate - 1, int(local.microsecond / 1e6 * rate))


def artnet_packet(h, m, s, f, fps):
    """ArtTimeCode: 'Art-Net\\0', OpCode (low byte first), protocol version 14, two filler bytes, F S M H, Type."""
    return (b"Art-Net\x00" + struct.pack("<H", ARTNET_OPCODE_TIMECODE) + bytes([0, 14, 0, 0, f, s, m, h, ARTNET_TYPE[fps]]))


def fmt(tc):
    return "%02d:%02d:%02d:%02d" % tc


def vlan_addresses():
    """{interface: (ipv4, broadcast)} from the kernel. The broadcast address is worked out from address and prefix when
    the kernel has none recorded for the address."""
    try:
        data = json.loads(subprocess.run(["ip", "-j", "-4", "addr"], capture_output=True, text=True, timeout=3).stdout or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}
    out = {}
    for i in data:
        for a in i.get("addr_info", []):
            if a.get("family") != "inet" or "local" not in a:
                continue
            bcast = a.get("broadcast")
            if not bcast and 0 <= a.get("prefixlen", 32) <= 30:
                try:
                    bcast = str(ipaddress.ip_interface(f"{a['local']}/{a['prefixlen']}").network.broadcast_address)
                except ValueError:
                    bcast = None
            if bcast:
                out[i["ifname"]] = (a["local"], bcast)
    return out


class Sender:
    """One Art-Net output per VLAN that has it enabled."""

    def __init__(self, cfg_path, status_path, dest_override=None, now=time.time, addresses=vlan_addresses):
        self.cfg_path, self.status_path, self.dest_override = cfg_path, status_path, dest_override
        self.now, self.addresses = now, addresses
        self.outputs = {}                  # vlan id -> dict(state, fps, tz, name, iface, next, sent, last, dest, sock)
        self.cfg_mtime = None
        self.addr_at = 0
        self.addr = {}
        self.written = 0

    # ---- configuration
    def reload(self):
        try:
            mtime = os.stat(self.cfg_path).st_mtime
            if mtime == self.cfg_mtime:
                return
            cfg = config.load(self.cfg_path)
        except (OSError, ValueError, config.ConfigError) as e:
            if self.cfg_mtime is None:
                self.cfg_mtime = -1
                log.warning("no usable configuration yet (%s); sending nothing until there is one", e)
            return
        self.cfg_mtime = mtime
        self.cfg = cfg
        log.info("configuration loaded: %d VLANs, Art-Net on for %s", len(cfg["vlans"]),
                 [v["id"] for v in cfg["vlans"] if v["timecode"]["artnet"]] or "none")
        keep = {}
        for v in cfg["vlans"]:
            old = self.outputs.get(v["id"])
            on = v["timecode"]["artnet"]
            o = dict(old) if old else {"sent": 0, "last": None, "next": 0, "sock": None, "error": None}
            o.update(name=v["name"], iface=v["iface"], tz=v["timezone"], fps=v["timecode"]["fps"],
                     artnet=on, mtc=v["timecode"]["mtc_rtp"])
            keep[v["id"]] = o
        for vid, o in self.outputs.items():
            if vid not in keep and o.get("sock"):
                o["sock"].close()
        self.outputs = keep

    def _state(self, vid, o, state, detail=""):
        """Write every change of a VLAN's output state to the log (and nothing while it stays the same)."""
        if o.get("logged") != (state, detail):
            o["logged"] = (state, detail)
            msg = f"VLAN {vid} {o['name']}: Art-Net {state}{' ' + detail if detail else ''}"
            (log.warning if state in ("error", "no-address") else log.info)(msg)

    def refresh_addresses(self):
        if self.now() - self.addr_at >= 2:
            self.addr, self.addr_at = self.addresses(), self.now()

    # ---- sending
    def _socket(self, local_ip):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind((local_ip, 0))              # the source address picks the VLAN: the broadcast leaves on its interface
        return s

    def tick(self):
        """Send every frame that is due. Returns the time of the next due frame (or None when nothing is enabled)."""
        self.reload()
        self.refresh_addresses()
        t = self.now()
        nxt = None
        for vid, o in self.outputs.items():
            if not o["artnet"]:
                if o["sock"]:
                    o["sock"].close()
                    o["sock"] = None
                o["error"] = None
                self._state(vid, o, "off")
                continue
            rate = FRAME_RATE[o["fps"]]
            local_ip, bcast = self.addr.get(o["iface"], (None, None))
            if self.dest_override:
                local_ip, bcast = self.dest_override[0], self.dest_override[1]
            if not local_ip:
                o["error"], o["dest"] = None, None
                o["state"] = "no-address"
                self._state(vid, o, "no-address", "(waiting for an IP address on " + o["iface"] + ")")
                continue
            if o["sock"] is None or o.get("bound") != local_ip:
                try:
                    if o["sock"]:
                        o["sock"].close()
                    o["sock"], o["bound"] = self._socket(local_ip), local_ip
                except OSError as e:
                    o["sock"], o["error"], o["state"] = None, str(e), "error"
                    self._state(vid, o, "error", f"(cannot open a socket on {local_ip}: {e})")
                    continue
            o["dest"] = f"{bcast}:{ARTNET_PORT}" if not self.dest_override else f"{bcast}:{self.dest_override[2]}"
            if o["next"] == 0 or t - o["next"] > 1.0:          # first frame, or we fell behind: realign
                o["next"] = (int(t * rate) + 1) / rate
            while o["next"] <= t:
                # label the frame from 0.1 ms after its start: frame starts such as 1/24 s are not whole microseconds,
                # and rounding right on the boundary would repeat the previous frame number
                tc = timecode_at(o["next"] + 1e-4, o["tz"], o["fps"])
                try:
                    port = self.dest_override[2] if self.dest_override else ARTNET_PORT
                    o["sock"].sendto(artnet_packet(*tc, o["fps"]), (bcast, port))
                    o["sent"] += 1
                    o["last"], o["error"], o["state"] = fmt(tc), None, "sending"
                    self._state(vid, o, "sending", f"{o['fps']} fps, zone {o['tz']}, to {o['dest']}")
                except OSError as e:
                    o["error"], o["state"] = str(e), "error"
                    self._state(vid, o, "error", f"(send to {o['dest']} failed: {e})")
                o["next"] += 1 / rate
            nxt = o["next"] if nxt is None else min(nxt, o["next"])
        self.write_status()
        return nxt

    # ---- status for the WebUI
    def write_status(self, force=False):
        if not force and self.now() - self.written < 1.0:
            return
        self.written = self.now()
        vlans = []
        for vid, o in sorted(self.outputs.items()):
            a = ({"state": "off"} if not o["artnet"] else
                 {"state": o.get("state", "starting"), "fps": o["fps"], "dest": o.get("dest"), "sent": o["sent"],
                  "last": o["last"], "error": o["error"]})
            m = {"state": "not-implemented"} if o["mtc"] else {"state": "off"}
            vlans.append({"id": vid, "name": o["name"], "tz": o["tz"], "artnet": a, "mtc_rtp": m})
        doc = json.dumps({"time": self.now(), "vlans": vlans})
        os.makedirs(os.path.dirname(self.status_path), exist_ok=True)
        tmp = self.status_path + ".tmp"
        with open(tmp, "w") as f:
            f.write(doc)
        os.replace(tmp, self.status_path)

    def run(self):
        log.info("timecode sender started")
        failures = 0
        while True:
            try:
                nxt = self.tick()
                failures = 0
            except Exception:
                # never die on a bug: log it with its traceback, wait a moment (so a persistent fault cannot spin), carry on
                failures += 1
                log.exception("unexpected error in the sender loop (failure %d in a row)", failures)
                time.sleep(min(30, failures))
                continue
            wait = 0.5 if nxt is None else max(0.0, min(0.5, nxt - self.now()))
            time.sleep(wait)


def main(cfg_path, status_path, dest=None):
    Sender(cfg_path, status_path, dest_override=dest).run()
