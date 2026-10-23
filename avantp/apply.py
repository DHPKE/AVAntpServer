"""Root helper: puts the saved (pending) config on the system, with automatic rollback.

The unprivileged WebUI only writes pending.json and an apply.request file. This helper (started by a systemd path unit)
re-validates pending.json itself, so everything it writes comes from the validated config and nothing else:
  1. snapshot every file it will touch, 2. write the new files, 3. activate (sysctl, nftables, networkd, chrony),
  4. wait for the user to press Confirm in the WebUI, 5. if that does not happen in time, restore the snapshot.
A change that cuts the user off (wrong VLAN, wrong address) therefore undoes itself.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from . import config, render

CONFIRM_SECONDS = int(os.environ.get("AVANTP_CONFIRM_SECONDS", 90))   # the env override exists for tests
MAX_JSON = 256 * 1024


class Paths:
    """All locations in one place; `root` lets the tests run against a temporary tree."""

    def __init__(self, root="/", data="/var/lib/avantp", run="/run/avantp"):
        self.root = root
        self.data = data if root == "/" else os.path.join(root, data.lstrip("/"))
        self.run = run if root == "/" else os.path.join(root, run.lstrip("/"))
        self.etc = self.under("etc/avantp")

    def under(self, rel):
        return os.path.join(self.root, rel)


def _write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _read_json_nofollow(path):
    """pending.json lives in a directory the WebUI user owns: never follow a symlink planted there."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
        if os.fstat(f.fileno()).st_size > MAX_JSON:
            raise ValueError("pending.json too large")
        return json.load(f)


def _json_or(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def set_status(paths, **kw):
    kw["time"] = time.time()
    _write(os.path.join(paths.run, "status.json"), json.dumps(kw), 0o644)


# ---- snapshot / restore (files, symlinks and "did not exist")
def snapshot(paths, rels, snapdir):
    shutil.rmtree(snapdir, ignore_errors=True)
    os.makedirs(os.path.join(snapdir, "files"))
    meta = {}
    for rel in sorted(rels):
        p = paths.under(rel)
        if os.path.islink(p):
            meta[rel] = {"kind": "link", "target": os.readlink(p)}
        elif os.path.isfile(p):
            dst = os.path.join(snapdir, "files", rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(p, dst)
            meta[rel] = {"kind": "file"}
        else:
            meta[rel] = {"kind": "missing"}
    _write(os.path.join(snapdir, "meta.json"), json.dumps(meta))
    return meta


def restore(paths, snapdir, meta):
    for rel, m in meta.items():
        p = paths.under(rel)
        if os.path.lexists(p):
            os.unlink(p)
        if m["kind"] == "file":
            os.makedirs(os.path.dirname(p), exist_ok=True)
            shutil.copy2(os.path.join(snapdir, "files", rel), p)
        elif m["kind"] == "link":
            os.makedirs(os.path.dirname(p), exist_ok=True)
            os.symlink(m["target"], p)


def _ifaces(cfg):
    if not cfg:
        return set()
    return {f"{cfg['lan']['iface']}.{v['id']}" for v in cfg.get("vlans", [])}


def apply_once(paths, ex, confirm_seconds=CONFIRM_SECONDS, poll=0.5, now=time.time, sleep=time.sleep):
    """Apply paths.data/pending.json. Returns 'applied', 'rolled-back' or 'error'."""
    try:
        os.unlink(os.path.join(paths.data, "apply.request"))
    except FileNotFoundError:
        pass
    try:
        cfg = config.validate(_read_json_nofollow(os.path.join(paths.data, "pending.json")))
        files = render.render_all(cfg)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, config.ConfigError) as e:
        set_status(paths, state="error", error=f"pending config rejected: {e}")
        return "error"
    ok, why = ex.preflight(files)
    if not ok:
        set_status(paths, state="error", error=f"pre-flight check failed, nothing changed: {why}")
        return "error"

    confirm = os.path.join(paths.data, "confirm")
    managed_path = os.path.join(paths.etc, "managed.json")
    old_managed = _json_or(managed_path, [])
    old_cfg = _json_or(os.path.join(paths.etc, "config.json"), None)
    removed = sorted(_ifaces(old_cfg) - _ifaces(cfg))
    rels = set(files) | set(old_managed) | {"etc/avantp/config.json", "etc/avantp/managed.json"} | set(ex.extra_paths())
    snapdir = os.path.join(paths.etc, "rollback")
    meta = snapshot(paths, rels, snapdir)
    takeover = ex.takeover_needed()

    def roll_back(reason):
        restore(paths, snapdir, meta)
        try:
            ex.rollback(takeover, old_cfg)
        except Exception as e:  # keep going: files are already restored
            reason += f" (rollback commands reported: {e})"
        set_status(paths, state="rolled-back", reason=reason)
        return "rolled-back"

    set_status(paths, state="applying")
    try:
        for rel, text in files.items():
            _write(paths.under(rel), text)
        for rel in set(old_managed) - set(files):
            try:
                os.unlink(paths.under(rel))
            except FileNotFoundError:
                pass
        _write(managed_path, json.dumps(sorted(files)))
        if takeover:
            ex.takeover(cfg)
        ex.activate(cfg, removed, takeover)
    except Exception as e:
        return roll_back(f"activation failed: {e}")

    try:
        os.unlink(confirm)
    except FileNotFoundError:
        pass
    deadline = now() + confirm_seconds
    set_status(paths, state="pending-confirm", deadline=deadline, seconds=confirm_seconds)
    while now() < deadline:
        if os.path.lexists(confirm):
            break
        sleep(poll)
    else:
        return roll_back(f"not confirmed within {confirm_seconds} s, so the previous setup was restored")
    _write(os.path.join(paths.etc, "config.json"), json.dumps(config.dump(cfg), indent=1))
    try:
        os.unlink(confirm)
    except FileNotFoundError:
        pass
    set_status(paths, state="applied")
    return "applied"


# ---- executors: the only code that touches the running system
class FakeExecutor:
    """For tests and development: records what would run."""

    def __init__(self, fail_activate=False, fail_preflight=False, takeover=False):
        self.calls, self.fail_activate, self.fail_preflight, self._takeover = [], fail_activate, fail_preflight, takeover

    def extra_paths(self):
        return []

    def preflight(self, files):
        self.calls.append("preflight")
        return (False, "fake failure") if self.fail_preflight else (True, "")

    def takeover_needed(self):
        return self._takeover

    def takeover(self, cfg):
        self.calls.append("takeover")

    def activate(self, cfg, removed, takeover):
        self.calls.append(("activate", tuple(removed), takeover))
        if self.fail_activate:
            raise RuntimeError("fake activation failure")

    def rollback(self, takeover, old_cfg):
        self.calls.append(("rollback", takeover))


class SystemExecutor:
    def __init__(self, paths):
        self.p = paths

    def sh(self, *argv, check=True, timeout=60):
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            r = subprocess.CompletedProcess(argv, 127, "", f"{argv[0]}: command not found")
        if check and r.returncode:
            raise RuntimeError(f"{' '.join(argv)}: {(r.stderr or r.stdout).strip()[:300]}")
        return r

    def extra_paths(self):
        return ["etc/resolv.conf"]

    def preflight(self, files):
        """Let the real tools judge the new files before anything is touched."""
        with tempfile.TemporaryDirectory() as d:
            for name, rel in (("nft", "etc/avantp/nftables.nft"), ("chrony", "etc/chrony/chrony.conf")):
                with open(os.path.join(d, name), "w") as f:
                    f.write(files[rel])
            for argv in (["nft", "-c", "-f", os.path.join(d, "nft")], ["chronyd", "-p", "-f", os.path.join(d, "chrony")]):
                r = subprocess.run(argv, capture_output=True, text=True)
                if r.returncode:
                    return False, f"{argv[0]}: {(r.stderr or r.stdout).strip()[:300]}"
        return True, ""

    def takeover_needed(self):
        """The Debian installer leaves ifupdown in charge of the network; the generated files are for systemd-networkd."""
        return self.sh("systemctl", "is-enabled", "networking", check=False).stdout.strip() == "enabled"

    def takeover(self, cfg):
        self.sh("systemctl", "disable", "networking")
        self.sh("ifdown", "--force", cfg["wan"]["iface"], check=False)   # stops dhcpcd; networkd gets the lease next
        self.sh("systemctl", "enable", "--now", "systemd-resolved")
        link = self.p.under("etc/resolv.conf")          # from now on resolved (fed by networkd's DHCP) owns DNS
        os.makedirs(os.path.dirname(link), exist_ok=True)
        os.symlink("../run/systemd/resolve/stub-resolv.conf", link + ".avantp")
        os.replace(link + ".avantp", link)
        self.sh("systemctl", "enable", "systemd-networkd")

    def activate(self, cfg, removed, takeover):
        self.sh("sysctl", "-p", self.p.under("etc/sysctl.d/90-avantp.conf"))
        self.sh("nft", "-f", self.p.under("etc/avantp/nftables.nft"))
        for i in removed:
            self.sh("ip", "link", "delete", i, check=False)
        self.sh("systemctl", "enable", "--now", "systemd-resolved", "systemd-networkd")
        self.sh("networkctl", "reload")
        names = [cfg["wan"]["iface"], cfg["lan"]["iface"]] + [f"{cfg['lan']['iface']}.{v['id']}" for v in cfg["vlans"]]
        for n in names:
            self.sh("networkctl", "reconfigure", n, check=False)
        self.sh("systemctl", "enable", "chrony")
        self.sh("systemctl", "restart", "chrony")

    def rollback(self, takeover, old_cfg):
        """Files are already restored; make the running system match them again."""
        nft = self.p.under("etc/avantp/nftables.nft")
        if os.path.exists(nft):
            self.sh("nft", "-f", nft, check=False)
        else:
            self.sh("nft", "delete", "table", "inet", "avantp", check=False)
        if takeover:
            self.sh("systemctl", "disable", "--now", "systemd-networkd", "systemd-resolved", check=False)
            self.sh("systemctl", "enable", "networking", check=False)
            self.sh("ip", "addr", "flush", "dev", (old_cfg or {}).get("wan", {}).get("iface", "enp4s0"), check=False)
            self.sh("systemctl", "restart", "networking", check=False)
        else:
            self.sh("networkctl", "reload", check=False)
        self.sh("systemctl", "restart", "chrony", check=False)

