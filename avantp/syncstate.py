"""Whether the clock follows an upstream NTP server right now, and the watchdog's status file that feeds that verdict.
Pure and import-light: the unprivileged timecode sender reads the same file."""
import json
import os

LOCAL_REF = "7F7F0101"      # chrony's reference id when it serves its own clock ("local stratum 10 orphan")
WAN_FRESH_S = 30            # the watchdog writes every 5 s (a slow chronyc can stretch that to ~12 s); an older file means it is not running


def wan_status_path():
    d = os.environ.get("AVANTP_DIR")
    return os.path.join(d, "run-heal", "status.json") if d else "/run/avantp-heal/status.json"


def read_wan(now, path=None):
    """The watchdog's file ({"t", "route", "connected", "server", "serving", "stop_at"}): its probe of the internet path
    and whether the hold time is used up. None when it is missing or stale: then chrony's view alone decides."""
    try:
        with open(path or wan_status_path()) as f:
            w = json.load(f)
    except (OSError, ValueError):
        return None
    return w if isinstance(w, dict) and now - w.get("t", 0) <= WAN_FRESH_S else None


def sync_state(tracking, sources, now, wan=None):
    """Whether the clock follows an upstream server right now: the one place that decides it.

    `chronyc tracking` alone cannot tell: after every source has gone silent chrony keeps reporting the last stratum,
    reference and a leap status of "Normal" for days, and keeps the source selected until its reach register runs
    empty, which at a poll interval of up to 1024 s takes hours. So the watchdog's probe (`wan`) decides first whether
    the internet is reachable at all; then what counts is a source that is selected now ('*', which also covers a
    selected reference clock) and is not chrony's own local reference.
    state: stopped | offline (no connection) | synced | local (serving its own clock) | unsynced
    serving is False once the configured hold time without an upstream is used up (NTP, Art-Net and MTC are then
    stopped); stop_at is when that will happen (None: never, or synced)."""
    if tracking is None:
        return {"state": "stopped", "selected": None, "reachable": 0, "total": 0, "last_sync_age_s": None,
                "serving": True, "stop_at": None}
    selected = next((x for x in sources if x["state"] == "*"), None)
    if wan and not wan["connected"]:
        state = "offline"
    elif tracking["ref_id"] == LOCAL_REF:
        state = "local"
    elif selected and tracking["stratum"] < 16 and tracking["ref_id"] != "00000000" and tracking["leap"] != "Not synchronised":
        state = "synced"
    else:
        state = "unsynced"
    return {"state": state, "selected": selected["name"] if selected else None,
            "reachable": sum(1 for x in sources if x["reach"]), "total": len(sources),
            "last_sync_age_s": max(0, now - tracking["ref_time"]) if tracking["ref_time"] else None,
            "serving": (wan or {}).get("serving", True), "stop_at": (wan or {}).get("stop_at")}
