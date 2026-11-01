"""Read-only view of the system journal for the WebUI's Log tab. Nothing from the request reaches a shell or an
arbitrary journalctl option: the source and level are looked up in fixed tables, numbers are clamped integers."""
import datetime
import json
import re
import subprocess
import time

SOURCES = {
    "all": ["avantp-web", "avantp-apply", "avantp-update", "avantp-software", "avantp-timecode", "avantp-nftables",
            "avantp-recover", "avantp-heal"],
    "web": ["avantp-web"], "apply": ["avantp-apply", "avantp-recover"], "update": ["avantp-update"],
    "software": ["avantp-software"],
    "timecode": ["avantp-timecode"], "firewall": ["avantp-nftables"],
    "chrony": ["chrony", "avantp-heal"], "network": ["systemd-networkd", "systemd-resolved"],
    "kernel": None,                                   # -k: kernel messages, e.g. NIC link changes, hardware errors
}
LEVELS = {"all": None, "warning": 4, "error": 3}          # worst syslog number a line may have to be shown
GREP_TIMEOUT = 30        # a text search reads every entry of the boot: 9 s for 180 000 WebUI request lines on the box
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


# The Log tab colours a line red or yellow by its words (ui/app.js logCls). The level filter must agree with what the
# user sees: a line shown in red ("Art-Net error ...", "login failed") belongs under "Errors only" even when the journal
# stored it as info or warning (our own services print to stderr, which systemd stores as priority "info").
# Keep these two patterns in step with logCls.
ERR_TEXT = re.compile(r"traceback|exception|error|fail|panic|segfault|oom|crit|killed|unhandled", re.I)
ERR_NOT = re.compile(r"error: 0|no error", re.I)
WARN_TEXT = re.compile(r"warn|degraded|timed out|link is down|downgraded|denied|rolling back|not confirmed", re.I)
REQUEST = re.compile(r'"[A-Z]+ [^"]*"')                     # an access-log request: its URL may say level=error, that is no error
MAX_SCAN = 5000


def severity(entry):
    """0 (emergency) .. 7 (debug) for a journal entry: the journal's priority, or worse if the text looks like an error/warning."""
    try:
        prio = int(entry.get("PRIORITY", 6))
    except (TypeError, ValueError):
        prio = 6
    msg = REQUEST.sub("", _message(entry))
    if ERR_TEXT.search(msg) and not ERR_NOT.search(msg):
        prio = min(prio, 3)
    elif WARN_TEXT.search(msg):
        prio = min(prio, 4)
    return prio


def _message(entry):
    m = entry.get("MESSAGE", "")
    if isinstance(m, list):                                  # journald gives non-UTF-8 messages as a list of bytes
        try:
            return bytes(m).decode("utf-8", "replace")
        except (TypeError, ValueError):
            return ""
    return m if isinstance(m, str) else ""


def _line(entry):
    try:
        ts = datetime.datetime.fromtimestamp(int(entry["__REALTIME_TIMESTAMP"]) / 1e6).astimezone().isoformat(timespec="seconds")
    except (KeyError, ValueError, OSError):
        ts = "?"
    ident = entry.get("SYSLOG_IDENTIFIER") or entry.get("_COMM") or "?"
    pid = f"[{entry['_PID']}]" if entry.get("_PID") else ""
    return f"{ts} {entry.get('_HOSTNAME', '')} {ident}{pid}: {_message(entry)}"


def _stamp(raw):
    try:
        return int(json.loads(raw).get("__REALTIME_TIMESTAMP", 0))
    except (ValueError, TypeError, AttributeError):
        return 0


def tail(source="all", level="all", lines=200, boot=0, since=0):
    if source not in SOURCES or level not in LEVELS:
        raise ValueError("unknown source or level")
    lines = max(20, min(MAX_LINES, int(lines)))
    boot = int(boot)
    if not -5 <= boot <= 0:
        raise ValueError("boot must be 0 (this boot) down to -5")
    since = float(since)                                     # CLEAR in the WebUI: hide entries older than this (epoch seconds)
    if not 0 <= since < time.time() + 3600:
        raise ValueError("bad since")
    limit = LEVELS[level]                                    # None, or the worst acceptable syslog number (4 or 3)
    scan = lines if limit is None else MAX_SCAN
    args = ["-o", "json", "-n", str(scan), "-b", str(boot)]
    if SOURCES[source] is None:
        args.append("-k")
    for u in SOURCES[source] or []:
        args += ["-u", u]
    if limit is None:
        out, err = _journalctl(args)
    else:
        # Two searches over the whole boot instead of the last few thousand entries: what the journal itself stored at that
        # priority, and what only the words say (our services log to stderr = info). Merged, then judged by severity().
        words = "|".join(r.pattern for r in ((ERR_TEXT,) if limit == 3 else (ERR_TEXT, WARN_TEXT)))
        out1, err = _journalctl(args + ["-p", str(limit)])
        out2, err2 = _journalctl(args + ["--case-sensitive=false", "--grep", words], timeout=GREP_TIMEOUT)
        err = err or err2
        seen, rows = set(), []
        for raw in ((out1 or "") + (out2 or "")).splitlines():
            if raw not in seen:
                seen.add(raw)
                rows.append(raw)
        rows.sort(key=_stamp)
        out = "\n".join(rows)
    if err:
        return {"lines": [], "error": err}
    keep = []
    for raw in out.splitlines():
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if since:
            try:
                if int(entry.get("__REALTIME_TIMESTAMP", 0)) / 1e6 <= since:
                    continue
            except (TypeError, ValueError):
                continue
        if limit is None or severity(entry) <= limit:
            keep.append(_line(entry))
    text = "\n".join(keep[-lines:])[-MAX_BYTES:]
    return {"lines": [ln for ln in text.splitlines() if ln][-lines:], "error": None}


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
