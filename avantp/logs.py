"""Read-only view of the system journal for the WebUI's Log tab. Nothing from the request reaches a shell or an
arbitrary journalctl option: the source and level are looked up in fixed tables, numbers are clamped integers."""
import json
import subprocess
import time

SOURCES = {
    "all": ["avantp-web", "avantp-apply", "avantp-update", "avantp-timecode", "avantp-nftables", "avantp-recover"],
    "web": ["avantp-web"], "apply": ["avantp-apply", "avantp-recover"], "update": ["avantp-update"],
    "timecode": ["avantp-timecode"], "firewall": ["avantp-nftables"],
    "chrony": ["chrony"], "network": ["systemd-networkd", "systemd-resolved"],
    "kernel": None,                                   # -k: kernel messages, e.g. NIC link changes, hardware errors
}
LEVELS = {"all": None, "warning": "warning", "error": "err"}
MAX_LINES, MAX_BYTES = 1000, 400_000
_boots_cache = (0.0, None)


def _journalctl(args, timeout=8):
    try:
        r = subprocess.run(["journalctl", "--no-pager"] + args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"journalctl is not available: {e}"
    out = r.stdout
    if "insufficient permissions" in r.stderr or (r.returncode and not out):
        return None, ("no access to the journal: the WebUI user needs to be in the group systemd-journal "
                      "(run deploy/install.sh again)" if "permission" in r.stderr else (r.stderr.strip()[:300] or "journalctl failed"))
    return out, None


def tail(source="all", level="all", lines=200, boot=0):
    if source not in SOURCES or level not in LEVELS:
        raise ValueError("unknown source or level")
    lines = max(20, min(MAX_LINES, int(lines)))
    boot = int(boot)
    if not -5 <= boot <= 0:
        raise ValueError("boot must be 0 (this boot) down to -5")
    args = ["-o", "short-iso", "-n", str(lines), "-b", str(boot)]
    if SOURCES[source] is None:
        args.append("-k")
    for u in SOURCES[source] or []:
        args += ["-u", u]
    if LEVELS[level]:
        args += ["-p", LEVELS[level]]
    out, err = _journalctl(args)
    if err:
        return {"lines": [], "error": err}
    text = out[-MAX_BYTES:]
    return {"lines": [ln for ln in text.splitlines() if ln and not ln.startswith("-- ")][-lines:], "error": None}


CLEAN_END = ("Reached target reboot", "Reached target power-off", "Reached target final", "Shutting down", "systemd-shutdown")


def boots(now=time.time):
    """Recent boots and whether each one ended with a shutdown (clean) or just stopped (power loss, reset, kernel crash)."""
    global _boots_cache
    if _boots_cache[1] is not None and now() - _boots_cache[0] < 20:
        return _boots_cache[1]
    out, err = _journalctl(["--list-boots", "-o", "json"])
    if err:
        return {"boots": [], "error": err}
    try:
        items = json.loads(out)[-6:]
    except ValueError:
        return {"boots": [], "error": "could not read the boot list"}
    res = []
    last = len(items) - 1
    for i, b in enumerate(items):
        idx = b.get("index", i - last)
        entry = {"index": idx, "first": b.get("first_entry", 0) // 1_000_000, "last": b.get("last_entry", 0) // 1_000_000}
        if i == last:
            entry["ended"] = "running"
        else:
            tail_out, e = _journalctl(["-b", str(idx), "-n", "60", "-o", "cat"])
            entry["ended"] = "clean" if tail_out and any(m.lower() in tail_out.lower() for m in CLEAN_END) else "abrupt"
        res.append(entry)
    _boots_cache = (now(), {"boots": res, "error": None})
    return _boots_cache[1]
