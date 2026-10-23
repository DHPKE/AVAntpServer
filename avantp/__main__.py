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
    _, srv = webmod.serve(data_dir, host, port, cert if tls else None, key if tls else None, etc_dir=etc, run_dir=run)
    print("listening on %s:%d (%s)" % (host, port, "https" if tls else "HTTP, TESTING ONLY"))
    srv.serve_forever()


def main(argv):
    if len(argv) == 3 and argv[1] == "passwd":
        return passwd(paths()[0], argv[2])
    if len(argv) == 4 and argv[1] == "web":
        return web(paths()[0], argv[2], int(argv[3]))
    if len(argv) >= 2 and argv[1] == "apply":
        return run_apply(argv[2:])
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
