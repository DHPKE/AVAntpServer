"""Read-only system status: chrony state and network interfaces. All subprocess calls are fixed argv, no shell."""
import ipaddress
import json
import subprocess
import time

from . import names

def _run(argv, timeout=3):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def parse_tracking(csv):
    f = csv.strip().split(",")
    if len(f) < 14:
        return None
    return {
        "ref_id": f[0], "ref_name": f[1], "stratum": int(f[2]), "ref_time": float(f[3]),
        "offset_s": float(f[4]), "last_offset_s": float(f[5]), "rms_offset_s": float(f[6]),
        "freq_ppm": float(f[7]), "skew_ppm": float(f[9]), "root_delay_s": float(f[10]),
        "root_dispersion_s": float(f[11]), "update_interval_s": float(f[12]), "leap": f[13],
    }


def parse_sources(csv):
    out = []
    for line in csv.strip().splitlines():
        f = line.split(",")
        if len(f) < 10:
            continue
        out.append({"mode": f[0], "state": f[1], "name": f[2], "stratum": int(f[3]), "poll": int(f[4]),
                    "reach": int(f[5], 8), "last_rx_s": int(f[6]), "offset_s": float(f[7])})
    return out


def _is_ip(x):
    try:
        ipaddress.ip_address(x)
        return True
    except ValueError:
        return False


def add_hosts(tracking, sources, resolver=None):
    """Adds `host` (reverse DNS name, or None while unknown) next to each address chrony reported."""
    resolver = resolver or names.resolver
    if tracking:
        tracking["ref_host"] = resolver.get(tracking["ref_name"]) if _is_ip(tracking["ref_name"]) else tracking["ref_name"]
    for src in sources:
        src["host"] = resolver.get(src["name"]) if _is_ip(src["name"]) else src["name"]


def chrony():
    t = parse_tracking(_run(["chronyc", "-h", "127.0.0.1", "-c", "tracking"]))
    sources = parse_sources(_run(["chronyc", "-h", "127.0.0.1", "-c", "sources"])) if t else []
    add_hosts(t, sources)
    return {"running": t is not None, "tracking": t, "sources": sources}


def interfaces():
    try:
        data = json.loads(_run(["ip", "-j", "addr"]) or "[]")
    except ValueError:
        return []
    return [{"name": i["ifname"], "state": i.get("operstate", "?").lower(), "mac": i.get("address", ""),
             "addrs": [f"{a['local']}/{a['prefixlen']}" for a in i.get("addr_info", []) if a["family"] == "inet"]}
            for i in data if i["ifname"] != "lo"]


def snapshot():
    return {"utc": time.time(), "chrony": chrony(), "interfaces": interfaces()}
