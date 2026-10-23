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


def web(data_dir, host, port):
    from . import web as webmod
    cert, key = os.path.join(data_dir, "tls.crt"), os.path.join(data_dir, "tls.key")
    tls = os.path.exists(cert) and os.path.exists(key)
    if not tls and os.environ.get("AVANTP_ALLOW_HTTP") != "1":
        print("no TLS certificate (tls.crt/tls.key) in %s; set AVANTP_ALLOW_HTTP=1 for local testing only" % data_dir,
              file=sys.stderr)
        return 1
    _, srv = webmod.serve(data_dir, host, port, cert if tls else None, key if tls else None)
    print("listening on %s:%d (%s)" % (host, port, "https" if tls else "HTTP, TESTING ONLY"))
    srv.serve_forever()


def main(argv):
    if len(argv) == 3 and argv[1] == "passwd":
        return passwd(os.environ.get("AVANTP_DIR", "/etc/avantp"), argv[2])
    if len(argv) == 4 and argv[1] == "web":
        return web(os.environ.get("AVANTP_DIR", "/etc/avantp"), argv[2], int(argv[3]))
    if len(argv) != 4 or argv[1] != "render":
        print("usage: python -m avantp render <config.json> <outdir> | web <host> <port> | passwd <user>", file=sys.stderr)
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
