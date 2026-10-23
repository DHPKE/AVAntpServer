import logging
import os
import sys

from . import config, render


def passwd(data_dir, user):
    import getpass
    from . import auth
    pw = getpass.getpass(f"new password for {user}: ")
    if pw != getpass.getpass("again: "):
        print("passwords differ", file=sys.stderr)
        return 1
    try:
        auth.Users(os.path.join(data_dir, "users.json")).set_password(user, pw)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def paths():
    """(data dir of the WebUI user, root-owned dir with the active config, root-owned dir with the helper's status).
    With AVANTP_DIR set (development) everything lives below that one directory."""
    d = os.environ.get("AVANTP_DIR")
    if d:
        return d, os.path.join(d, "sysroot", "etc", "avantp"), os.path.join(d, "run")
    return "/var/lib/avantp", "/etc/avantp", "/run/avantp"


def run_timecode():
    """Art-Net timecode sender (unprivileged service). Development: AVANTP_TC_DEST=<local ip>,<dest ip>,<artnet port>[,<MTC control port>]."""
    from . import timecode
    _, etc, _ = paths()
    dest = os.environ.get("AVANTP_TC_DEST")
    if dest:
        ip, to, port, *mtc = dest.split(",")
        dest = (ip, to, int(port)) + ((int(mtc[0]),) if mtc else ())
    timecode.main(os.path.join(etc, "config.json"), timecode_status_path(), dest)


def timecode_status_path():
    d = os.environ.get("AVANTP_DIR")
    return os.path.join(d, "run-timecode", "status.json") if d else "/run/avantp-timecode/status.json"


def run_software(args):
    """One-shot (started by avantp-software.path): handle software.request. --fake --watch is the development loop:
    no firewall, GitHub answers come from AVANTP_DIR/fake-github.json and 'installing' only rewrites a VERSION file."""
    import time
    from . import apply, software
    data, _, run = paths()
    kw = {}
    if "--fake" in args:
        d = os.environ["AVANTP_DIR"]
        p = apply.Paths(root=os.path.join(d, "sysroot"))
        p.data, p.run = data, run
        os.makedirs(run, exist_ok=True)
        ex = apply.FakeExecutor()

        def fake_github(path):
            import json
            doc = json.load(open(os.path.join(d, "fake-github.json")))
            if "error" in doc:
                raise OSError(doc["error"])
            return doc.get(path.split("...")[0] if path.startswith("/compare/") else path)

        def fake_run(on_line):
            import json
            lines = []
            for step in ("git is not installed: installing it...", "first run: cloning x into y", "code: abc1234", "installing version fake"):
                time.sleep(1.3)                                  # slower than the page's 2 s refresh, like a real install
                lines.append(step)
                on_line(list(lines))
            new = json.load(open(os.path.join(d, "fake-github.json")))["/commits/main"]["sha"][:7]
            v = p.under("opt/avantp/VERSION")
            os.makedirs(os.path.dirname(v), exist_ok=True)
            open(v, "w").write(new)
            lines.append("installed version " + new)
            return 0, lines
        kw = {"fetch": fake_github, "runner": fake_run, "fresh": lambda: None}
    else:
        p = apply.Paths()
        ex = apply.SystemExecutor(p)
    while True:
        if os.path.lexists(os.path.join(p.data, "software.request")):
            print("software:", software.handle(p, ex, **kw), flush=True)
        if "--watch" not in args:
            return 0
        time.sleep(0.5)


def run_update_mode(args):
    """One-shot (started by avantp-update.path): handle update.request. With --fake --watch: development loop."""
    import time
    from . import apply, updatemode
    data, _, run = paths()
    if "--fake" in args:
        p = apply.Paths(root=os.path.join(os.environ["AVANTP_DIR"], "sysroot"))
        p.data, p.run = data, run
        os.makedirs(run, exist_ok=True)
        ex = apply.FakeExecutor()
    else:
        p = apply.Paths()
        ex = apply.SystemExecutor(p)
    while True:
        if os.path.lexists(os.path.join(p.data, "update.request")):
            print("update mode:", updatemode.handle(p, ex), flush=True)
        if "--watch" not in args:
            return 0
        time.sleep(0.5)


def run_apply(args):
    import time
    from . import apply
    data, _, run = paths()
    if "--fake" in args:   # development: records instead of touching the system; files go to AVANTP_DIR/sysroot
        p = apply.Paths(root=os.path.join(os.environ["AVANTP_DIR"], "sysroot"))
        p.data, p.run = data, run
        os.makedirs(run, exist_ok=True)
        ex = apply.FakeExecutor()
    else:
        p = apply.Paths()
        ex = apply.SystemExecutor(p)
    while True:
        if os.path.lexists(os.path.join(p.data, "apply.request")):
            print("apply:", apply.apply_once(p, ex), flush=True)
        if "--watch" not in args:
            return 0
        time.sleep(0.5)


def web(data_dir, host, port):
    from . import web as webmod
    cert, key = os.path.join(data_dir, "tls.crt"), os.path.join(data_dir, "tls.key")
    tls = os.path.exists(cert) and os.path.exists(key)
    if not tls and os.environ.get("AVANTP_ALLOW_HTTP") != "1":
        print("no TLS certificate (tls.crt/tls.key) in %s; set AVANTP_ALLOW_HTTP=1 for local testing only" % data_dir,
              file=sys.stderr)
        return 1
    _, etc, run = paths()
    _, srv = webmod.serve(data_dir, host, port, cert if tls else None, key if tls else None, etc_dir=etc, run_dir=run,
                          tc_status=timecode_status_path())
    print("listening on %s:%d (%s)" % (host, port, "https" if tls else "HTTP, TESTING ONLY"))
    srv.serve_forever()


def main(argv):
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(name)s: %(levelname)s %(message)s")
    if len(argv) == 3 and argv[1] == "passwd":
        return passwd(paths()[0], argv[2])
    if len(argv) == 4 and argv[1] == "web":
        return web(paths()[0], argv[2], int(argv[3]))
    if len(argv) >= 2 and argv[1] == "apply":
        return run_apply(argv[2:])
    if len(argv) == 2 and argv[1] == "recover":
        from . import apply
        print("recover:", apply.recover(apply.Paths()), flush=True)
        return 0
    if len(argv) == 2 and argv[1] == "timecode":
        return run_timecode()
    if len(argv) >= 2 and argv[1] == "software":
        return run_software(argv[2:])
    if len(argv) >= 2 and argv[1] == "update-mode":
        return run_update_mode(argv[2:])
    if len(argv) != 4 or argv[1] != "render":
        print("usage: python -m avantp render <config.json> <outdir> | web <host> <port> | passwd <user> | apply [--fake] [--watch]", file=sys.stderr)
        return 2
    try:
        cfg = config.load(argv[2])
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    for rel, body in render.render_all(cfg).items():
        p = os.path.join(argv[3], rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(body)
        print(p)
    return 0


sys.exit(main(sys.argv))
