"""Software update from the WebUI: look for a newer version on GitHub, or install it.

Same pattern as the other root helpers: the unprivileged WebUI only writes software.request ("check" or "update");
this helper (avantp-software.path/.service, root) reads nothing else from it. For the download it opens the firewall's
update window (web access on the WAN, with a kernel timeout), talks to GitHub at one fixed address, and closes the
window again whatever happens. The update itself is deploy/update.sh (git pull + install.sh), the same as typing it.

Progress and the result go to run/software.json, which survives the WebUI restart that an update causes, so the page
can pick up where it left off.
"""
import json
import logging
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

from . import apply

log = logging.getLogger("software")

REPO = "DHPKE/AVAntpServer"
API = f"https://api.github.com/repos/{REPO}"
CHECK_WINDOW, UPDATE_WINDOW = 120, 900         # seconds web access may stay open (the kernel closes it by itself)
UPDATE_SCRIPT = "/opt/avantp/deploy/update.sh"
HINT = "Could not reach GitHub. Check that the box has internet access on its WAN port."


def github(path, timeout=10):
    """GET https://api.github.com/repos/<repo><path> as parsed JSON. The only network call this module makes."""
    req = urllib.request.Request(API + path, headers={"User-Agent": "AVAntpServer", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def installed(paths):
    try:
        with open(paths.under("opt/avantp/VERSION")) as f:
            return f.read().strip()[:60] or "unknown"
    except OSError:
        return "unknown"


def compare(inst, fetch=github):
    """What GitHub's main looks like compared with the installed version `inst` (e.g. 'b47ea20' or '0021bf5-dirty')."""
    head = fetch("/commits/main")
    sha = head["sha"]
    msg = (head.get("commit", {}).get("message") or "").splitlines()[0][:140] if head.get("commit") else ""
    out = {"latest": sha[:7], "latest_full": sha, "message": msg, "date": (head.get("commit", {}).get("committer") or {}).get("date", ""),
           "installed": inst, "state": "unknown", "ahead_by": 0}
    base = re.match(r"^[0-9a-f]{7,40}", inst or "")
    if not base or inst.endswith("-dirty"):
        out["state"] = "local-build"                       # copied from a working tree: not a GitHub version
        return out
    base = base.group(0)
    if sha.startswith(base):
        out["state"] = "uptodate"
        return out
    try:
        cmp = fetch(f"/compare/{base}...{sha}")
    except urllib.error.HTTPError as e:
        out["state"] = "local-build" if e.code in (404, 422) else "unknown"      # GitHub has never seen that commit
        return out
    st = cmp.get("status")
    out["state"] = {"ahead": "newer", "identical": "uptodate", "behind": "local-newer", "diverged": "diverged"}.get(st, "unknown")
    out["ahead_by"] = int(cmp.get("ahead_by", 0) or 0)
    return out


def _status(paths, **kw):
    prev = apply._json_or(os.path.join(paths.run, "software.json"), {})
    doc = {"check": prev.get("check"), "installed": installed(paths), "time": time.time()}
    doc.update(kw)
    apply._write(os.path.join(paths.run, "software.json"), json.dumps(doc))
    return doc


def _window(paths, ex, on, seconds):
    cfg = apply._json_or(os.path.join(paths.etc, "config.json"), None)
    if not cfg:
        return False                                       # no firewall loaded yet: nothing to open
    try:
        ex.update_window(cfg["wan"]["iface"], on, seconds)
        return True
    except Exception as e:
        log.warning("update window %s failed: %s", "open" if on else "close", e)
        return False


def run_update_script(on_line, timeout=900):
    """Run deploy/update.sh and stream its output. Returns (exit code, last lines)."""
    lines = []
    p = subprocess.Popen([UPDATE_SCRIPT], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    deadline = time.time() + timeout
    for line in p.stdout:
        line = line.rstrip()
        if line:
            lines.append(line[:300])
            on_line(lines[-12:])
        if time.time() > deadline:
            p.kill()
            lines.append("stopped: took longer than %d s" % timeout)
            break
    return p.wait(), lines[-12:]


STEPS = (("installed version", "Done"), ("installing version", "Installing"), ("code:", "Downloaded"), ("cloning", "Downloading"),
         ("git is not installed", "Installing git"))


def step_of(lines):
    for needle, label in STEPS:
        if any(needle in l for l in lines):
            return label
    return "Working"


def handle(paths, ex, fetch=github, runner=run_update_script, now=time.time):
    """Process paths.data/software.request. Returns the resulting state."""
    req = os.path.join(paths.data, "software.request")
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
    if word not in ("check", "update"):
        _status(paths, state="error", error="invalid request")
        return "error"
    inst = installed(paths)
    if apply._json_or(os.path.join(paths.run, "status.json"), {}).get("state") in ("applying", "pending-confirm"):
        _status(paths, state="error", error="an apply is waiting for confirmation: finish that first")
        return "error"
    log.info("software %s requested (installed: %s)", word, inst)
    opened = False
    try:
        _status(paths, state="checking" if word == "check" else "updating", step="Opening web access")
        opened = _window(paths, ex, True, CHECK_WINDOW if word == "check" else UPDATE_WINDOW)
        _status(paths, state="checking" if word == "check" else "updating", step="Asking GitHub for the latest version")
        try:
            result = compare(inst, fetch)
        except Exception as e:
            log.error("cannot reach GitHub: %s", e)
            _status(paths, state="error", error=f"{HINT} ({str(e)[:120]})")
            return "error"
        if word == "check":
            _status(paths, state="checked", check=result)
            return "checked"
        if result["state"] == "uptodate":
            _status(paths, state="uptodate", check=result)
            return "uptodate"
        _status(paths, state="updating", check=result, step="Downloading", log=[])
        try:
            code, tail = runner(lambda lines: _status(paths, state="updating", check=result, step=step_of(lines), log=lines))
        except Exception as e:
            log.exception("the update script could not be run")
            _status(paths, state="error", check=result, error=f"the update script could not be run: {str(e)[:150]}")
            return "error"
        new = installed(paths)
        if code != 0:
            hint = next((l for l in reversed(tail) if "Update mode" in l or "fatal" in l or "rror" in l), "the update script failed")
            log.error("update failed (exit %s): %s", code, hint)
            _status(paths, state="error", check=result, error=f"update failed: {hint}", log=tail)
            return "error"
        log.info("updated: %s -> %s", inst, new)
        _status(paths, state="done", check=result, step="Done", log=tail, from_version=inst, installed=new)
        return "done"
    finally:
        if opened:
            _window(paths, ex, False, 0)                   # always: even after an error, the window must not stay open
