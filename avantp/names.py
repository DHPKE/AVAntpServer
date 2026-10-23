"""Reverse DNS for display: turns the IP addresses chrony reports into host names.

Lookups run in the background (one at a time, 3 s limit each) and are cached, so the status page never waits for DNS:
the first poll shows the address, the next ones show the name. A lookup is `getent hosts <ip>`, i.e. the system
resolver (systemd-resolved on the box), run without a shell. Names are display text only and are sanitised.
"""
import ipaddress
import queue
import re
import subprocess
import threading
import time

TTL, NEGATIVE_TTL = 3600, 600
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$")


def _getent(ip):
    try:
        out = subprocess.run(["getent", "hosts", ip], capture_output=True, text=True, timeout=3).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        return None
    # "91.206.8.70  s1.holub.co.at  alias..." - take the first name that is not just the address again
    for name in out[1:]:
        name = name.rstrip(".")
        if HOST_RE.match(name) and name != ip:
            return name
    return None


class Resolver:
    def __init__(self, lookup=_getent, now=time.time):
        self.lookup, self.now = lookup, now
        self.cache, self.pending = {}, set()
        self.lock = threading.Lock()
        self.q = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    def get(self, ip):
        """The host name for an IP if it is known already, else None (and a lookup is started)."""
        try:
            ip = str(ipaddress.ip_address(ip))
        except ValueError:
            return None
        with self.lock:
            hit = self.cache.get(ip)
            if hit and hit[1] > self.now():
                return hit[0]
            if ip not in self.pending:
                self.pending.add(ip)
                self.q.put(ip)
        return hit[0] if hit else None            # a stale name is better than flickering back to the address

    def _worker(self):
        while True:
            ip = self.q.get()
            try:
                name = self.lookup(ip)
            except Exception:
                name = None
            with self.lock:
                self.cache[ip] = (name, self.now() + (TTL if name else NEGATIVE_TTL))
                self.pending.discard(ip)


resolver = Resolver()
