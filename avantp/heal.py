"""Self-healing and live connection check for the upstream sync, without anyone pressing anything.

Two jobs, one loop (every TICK seconds):
1. Probe: one plain SNTP request to one upstream server, rotating. chronyd cannot tell this fast: it polls a silent
   server less and less often (up to every 1024 s) and keeps it selected until its reach register runs empty, so after
   unplugging the cable it still looks synced for hours, and after plugging it back it needs up to ~17 minutes. The
   result goes to a small status file that the WebUI shows (syncstate.read_wan).
2. Nudge: whenever the internet is reachable but the clock follows no upstream server, `chronyc burst` makes chronyd
   poll all sources at once: when the connection comes back, then at growing intervals while it stays unsynced.
3. Hold time (upstream.holdover_hours, 0 = never): that long after the last sync with no working upstream, the box stops
   answering NTP (chronyc deny) and tells the timecode sender to stop Art-Net and MTC (`serving` in the status file).
   Everything resumes by itself when the clock is synced again. The count restarts when this service restarts, except
   that chrony's own time of its last update is used if it has one.
It never edits a config file."""
import json
import logging
import os
import socket
import subprocess
import time

from . import config, render, status, syncstate

log = logging.getLogger("heal")

TICK = 5         # seconds between looks; also the probe rate (one 48-byte packet, to a different server each time)
FAILS = 2        # lost probes in a row before the connection counts as gone (one lost packet is not an outage)
FIRST = 30       # first gap between nudges while unsynced
MAX = 300        # longest gap: 17 sources x 4 packets every 5 minutes is nothing for a pool


def has_default_route(path="/proc/net/route"):
    try:
        with open(path) as f:
            next(f, None)
            for line in f:
                c = line.split()
                if len(c) > 3 and c[1] == "00000000" and int(c[3], 16) & 1:   # destination 0.0.0.0, RTF_UP
                    return True
    except (OSError, ValueError):
        pass
    return False


def sntp(server, timeout=1.0):
    """True if the server answers an SNTP request with a real time (a stratum 0 answer is a kiss-of-death)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(b"\x23" + bytes(47), (server, 123))        # version 4, mode 3 (client)
        data, _ = s.recvfrom(512)
    except OSError:
        return False
    finally:
        s.close()
    return len(data) >= 48 and data[0] & 7 == 4 and data[1] != 0


class Link:
    """Is the internet reachable right now: a default route and recent probe answers."""

    def __init__(self):
        self.i = 0
        self.fails = 0
        self.server = None
        self.reason = ""        # why it is down, for the log

    def step(self, route, servers, ask=sntp):
        """Returns connected (bool). `servers` are the addresses chrony uses; with none, the last verdict stands."""
        if not route:
            self.fails, self.reason = FAILS, "no default route: the WAN link is down or has no gateway"
        elif servers:
            self.server = servers[self.i % len(servers)]
            self.i += 1
            self.fails = 0 if ask(self.server) else min(self.fails + 1, FAILS)
            self.reason = f"{FAILS} NTP probes in a row got no answer (last tried {self.server})"
        return self.fails < FAILS


class Healer:
    def __init__(self):
        self.was_online = False
        self.reset()

    def reset(self):
        self.next_at = None
        self.gap = FIRST

    def step(self, state, online, now):
        """True when chronyd should be nudged now. `state` is syncstate.sync_state()['state']."""
        came_back = online and not self.was_online
        self.was_online = online
        if state in ("synced", "stopped") or not online:   # stopped is systemd's job; offline: nothing to reach
            self.reset()
            return False
        if came_back or self.next_at is None:
            self.gap, self.next_at = FIRST, now + FIRST
            return True
        if now >= self.next_at:
            self.gap = min(self.gap * 2, MAX)
            self.next_at = now + self.gap
            return True
        return False


class Holdover:
    """Time since the clock last followed an upstream server, against the configured hold time."""

    def __init__(self, last_ok):
        self.last_ok = last_ok

    def step(self, state, hours, now):
        """(serving, stop_at). `hours` None: no limit; 0: stop as soon as the clock follows no upstream. stop_at is None
        when there is no limit or the clock is synced."""
        if state == "synced":
            self.last_ok = now
        if hours is None or state == "synced":
            return True, None
        stop_at = self.last_ok + hours * 3600
        return now < stop_at, stop_at


class Settings:
    """upstream.holdover_hours and chronyd's allow rules from the active config, re-read when the file changes."""

    def __init__(self, path):
        self.path, self.mtime, self.hours, self.access = path, None, None, None

    def reload(self):
        try:
            mtime = os.stat(self.path).st_mtime
            if mtime == self.mtime:
                return
            cfg = config.load(self.path, strict=False)
        except (OSError, ValueError, config.ConfigError):
            return                                   # keep the last good values (none yet: never stop, restore nothing)
        self.mtime, self.hours, self.access = mtime, cfg["upstream"]["holdover_hours"], render.chrony_access(cfg)


def chronyc(*args):
    try:
        r = subprocess.run(["chronyc", *args], capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def nudge():
    return chronyc("burst", "4/4")


class Access:
    """Whether chronyd answers NTP clients. A stop is repeated on every look, so a chronyd restart (an apply) cannot
    reopen it; the way back is the configured allow rules, once."""

    def __init__(self):
        self.open = None                             # unknown until the first look

    def step(self, serving, rules):
        """The chronyc commands to run now."""
        if not serving:
            self.open = False
            return [("deny", "all")]
        if self.open is True or rules is None:
            return []
        self.open = True
        return [tuple(r.split()) for r in rules]


def ago(seconds):
    """12 s, 5 min, 2.0 h, 3.1 d: the unit that reads well."""
    for limit, unit, div in ((90, "s", 1), (5400, "min", 60), (172800, "h", 3600)):
        if seconds < limit:
            return f"{seconds / div:.0f} {unit}" if div < 3600 else f"{seconds / div:.1f} {unit}"
    return f"{seconds / 86400:.1f} d"


def events(prev, cur):
    """What to write to the journal when the connection, the sync state or the hold time changed. `prev` is None on the
    first look. Returns [(level, message)]: a loss is a warning, coming back is info."""
    out = []
    if prev is None or cur["connected"] != prev["connected"]:
        out.append((logging.INFO, "connection to the internet: an upstream NTP server answers") if cur["connected"]
                   else (logging.WARNING, f"connection to the internet lost: {cur['reason']}"))
    if prev is None or cur["state"] != prev["state"]:
        if cur["state"] == "synced":
            out.append((logging.INFO, f"clock synchronised to {cur['selected']}"))
        else:
            why = {"offline": "no connection", "unsynced": "no upstream server selected", "local": "serving its own clock (stratum 10)",
                   "stopped": "chronyd is not running"}[cur["state"]]
            age = f"last sync {ago(cur['age'])} ago" if cur["age"] is not None else "never synchronised"
            seen = "" if cur["state"] == "offline" else f"{cur['reachable']} of {cur['total']} sources reachable, "   # chronyd's reach lags behind a lost link
            out.append((logging.WARNING, f"clock is not synchronised to an upstream server ({why}; {seen}{age})"))
    if prev is not None and cur["serving"] != prev["serving"]:
        out.append((logging.INFO, "clock follows an upstream server again (or the hold time was raised): NTP, Art-Net and MTC are served again")
                   if cur["serving"] else
                   (logging.WARNING, "hold time without an upstream is used up: NTP, Art-Net and MTC are stopped"))
    return out


def write_status(path, route, connected, server, serving, stop_at):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"t": time.time(), "route": route, "connected": connected, "server": server,
                   "serving": serving, "stop_at": stop_at}, f)
    os.replace(tmp, path)


class Watchdog:
    """One look every TICK: probe the internet, decide the verdict and the hold time, open or close NTP, nudge chronyd,
    write the status file and the journal."""

    def __init__(self, cfg_path):
        self.path = syncstate.wan_status_path()
        self.link, self.healer, self.access, self.settings = Link(), Healer(), Access(), Settings(cfg_path)
        self.holdover = self.prev = None
        self.access_refused = False

    def look(self, now):
        self.settings.reload()
        c = status.chrony(hosts=False)
        if self.holdover is None:
            self.holdover = Holdover((c["tracking"] or {}).get("ref_time") or now)
        route = has_default_route()
        servers = [x["name"] for x in c["sources"] if status.is_ip(x["name"]) and ":" not in x["name"]]
        if route and not servers:
            chronyc("refresh")      # after a boot without network chronyd resolves its server names with a growing back-off: try now
        connected = self.link.step(route, servers)
        sync = c["sync"]
        serving, stop_at = self.holdover.step(sync["state"], self.settings.hours, now)
        write_status(self.path, route, connected, self.link.server, serving, stop_at)
        cur = {"connected": connected, "reason": self.link.reason, "state": sync["state"], "selected": sync["selected"],
               "reachable": sync["reachable"], "total": sync["total"], "age": sync["last_sync_age_s"], "serving": serving}
        for level, msg in events(self.prev, cur):
            log.log(level, msg)
        self.prev = cur
        cmds = self.access.step(serving, self.settings.access)
        refused = [" ".join(cmd) for cmd in cmds if not chronyc(*cmd)]
        if refused and not self.access_refused:                      # once per spell, not every look while chronyd is down
            log.warning("chronyd refused: chronyc %s (NTP access is not as intended until it accepts)", ", ".join(refused))
        self.access_refused = bool(refused)
        if self.healer.step(sync["state"], connected, time.time()):
            log.info("connected, but no upstream selected (%d of %d reachable): polling all sources now (%s)",
                     sync["reachable"], sync["total"], "ok" if nudge() else "chronyc refused")


def main(cfg_path):
    watchdog, failures = Watchdog(cfg_path), 0
    while True:
        started = time.time()
        try:
            watchdog.look(started)
            failures = 0
        except Exception:
            # never die on a bug (the sync verdict and the hold time depend on this loop): log it, wait a moment, carry on
            failures += 1
            log.exception("unexpected error in the watchdog loop (failure %d in a row)", failures)
            time.sleep(min(30, failures))
            continue
        time.sleep(max(0.0, TICK - (time.time() - started)))
