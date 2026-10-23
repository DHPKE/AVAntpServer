"""Update mode: lets the box reach package mirrors (outbound TCP 80/443 on the WAN) for a limited time.

The firewall normally allows the box only NTP, DNS and DHCP towards the WAN, so apt cannot work. The WebUI (no root)
writes update.request with the word "on" or "off"; this root helper adds or removes the WAN interface in the
nftables set `update_window`. The element carries a kernel timeout, so the window closes by itself even if nobody
presses the button or this helper never runs again. Nothing but the two words is read from the request.
"""
import json
import os
import time

from . import apply

MINUTES = 15


def handle(paths, ex, now=time.time):
    """Process paths.data/update.request. Returns 'on', 'off' or 'error'."""
    req = os.path.join(paths.data, "update.request")
    status = os.path.join(paths.run, "update.json")
    try:
        fd = os.open(req, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as f:
            word = f.read(16).strip()
    except OSError:
        word = ""
    try:
        os.unlink(req)
    except FileNotFoundError:
        pass
    if word not in ("on", "off"):
        apply._write(status, json.dumps({"state": "error", "error": "invalid request", "time": now()}))
        return "error"
    cfg = apply._json_or(os.path.join(paths.etc, "config.json"), None)
    if not cfg:
        apply._write(status, json.dumps({"state": "error", "time": now(),
                                         "error": "no active configuration yet: apply one first, the firewall is not loaded"}))
        return "error"
    try:
        ex.update_window(cfg["wan"]["iface"], word == "on", MINUTES * 60)
    except Exception as e:
        apply._write(status, json.dumps({"state": "error", "error": str(e)[:300], "time": now()}))
        return "error"
    until = now() + MINUTES * 60
    apply._write(status, json.dumps({"state": "on", "until": until, "time": now()} if word == "on"
                                    else {"state": "off", "time": now()}))
    return word


def close_after_apply(paths, now=time.time):
    """A firewall reload replaces the whole table, so an open window is gone: keep the status truthful."""
    status = os.path.join(paths.run, "update.json")
    if apply._json_or(status, {}).get("state") == "on":
        apply._write(status, json.dumps({"state": "off", "time": now(), "note": "closed by a firewall reload"}))
