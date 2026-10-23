"""WebUI backend: stdlib only. Serves ui/, a JSON API behind a login, optionally over TLS.

Config flow: the browser edits a config, PUT /api/config validates it and stores it as pending.json.
POST /api/apply asks the separate root helper (avantp apply) to put it on the system; the helper reports in status.json
and rolls back by itself unless POST /api/confirm arrives in time. This process never needs root.
"""
import copy
import http.server
import json
import os
import ssl
import sys
import threading
import time
from http import cookies

from . import auth, config, status

UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript"),
          "/vue.js": ("vue.global.prod.js", "text/javascript")}
CSP = ("default-src 'none'; script-src 'self' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; "
       "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
MAX_BODY = 256 * 1024


def version():
    try:
        with open(os.path.join(os.path.dirname(UI_DIR), "VERSION")) as f:
            return f.read().strip()[:40] or "unknown"
    except OSError:
        return "unknown"


class App:
    def __init__(self, data_dir, tls, etc_dir=None, run_dir=None):
        self.dir = data_dir
        self.etc = etc_dir or data_dir   # root-owned: the active config
        self.run = run_dir or data_dir   # root-owned: the helper's status
        self.tls = tls
        self.users = auth.Users(os.path.join(data_dir, "users.json"))
        self.sessions = auth.Sessions()
        self.lock = threading.Lock()

    def _read(self, name, base=None):
        try:
            with open(os.path.join(base or self.dir, name)) as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def active(self):
        return self._read("config.json", self.etc)

    def update_status(self):
        st = self._read("update.json", self.run) or {"state": "off"}
        if st.get("state") == "on" and st.get("until", 0) < time.time():   # the kernel closed it
            st = {"state": "off", "time": st.get("until")}
        return st

    def apply_status(self):
        return self._read("status.json", self.run) or {"state": "idle"}

    def pending(self):
        return self._read("pending.json")

    def save_pending(self, cfg):
        p = os.path.join(self.dir, "pending.json")
        with open(p + ".tmp", "w") as f:
            json.dump(cfg, f, indent=1)
        os.replace(p + ".tmp", p)


def make_handler(app):
    class H(http.server.BaseHTTPRequestHandler):
        server_version = "avantp"
        sys_version = ""

        def log_message(self, fmt, *a):
            sys.stderr.write("%s %s\n" % (self.client_address[0], fmt % a))

        # ---- helpers
        def send(self, code, body, ctype="application/json", extra=()):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if app.tls:
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def err(self, code, msg):
            self.send(code, {"error": msg})

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("body too large")
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                raise ValueError("invalid JSON")

        def session(self):
            c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            tok = c["sid"].value if "sid" in c else None
            return tok, (app.sessions.get(tok) if tok else None)

        def cookie(self, tok, max_age):
            flags = "HttpOnly; SameSite=Strict; Path=/; Max-Age=%d" % max_age
            return ("Set-Cookie", f"sid={tok}; {flags}" + ("; Secure" if app.tls else ""))

        # ---- routing
        def do_GET(self):
            path = self.path.split("?")[0]
            if path in STATIC:
                name, ctype = STATIC[path]
                with open(os.path.join(UI_DIR, name), "rb") as f:
                    return self.send(200, f.read(), ctype)
            tok, s = self.session()
            if path == "/api/session":
                return self.send(200, {"user": s["user"], "csrf": s["csrf"], "version": version()} if s else {"user": None,
                                 "needs_setup": not app.users.any()})
            if not s:
                return self.err(401, "login required")
            if path == "/api/status":
                return self.send(200, status.snapshot())
            if path == "/api/config":
                return self.send(200, {"active": app.active(), "pending": app.pending()})
            if path == "/api/apply":
                return self.send(200, app.apply_status())
            if path == "/api/update-mode":
                return self.send(200, app.update_status())
            self.err(404, "not found")

        def do_POST(self):
            self.mutate(post=True)

        def do_PUT(self):
            self.mutate(post=False)

        def mutate(self, post):
            path = self.path.split("?")[0]
            ip = self.client_address[0]
            try:
                data = self.body()
            except ValueError as e:
                return self.err(400, str(e))
            if post and path == "/api/login":
                if app.sessions.throttled(ip):
                    return self.err(429, "too many failed logins, wait a few minutes")
                u, p = str(data.get("user", "")), str(data.get("password", ""))
                if not app.users.check(u, p):
                    app.sessions.fail(ip)
                    return self.err(401, "wrong user or password")
                tok, csrf = app.sessions.create(u)
                return self.send(200, {"user": u, "csrf": csrf, "version": version()}, extra=[self.cookie(tok, auth.SESSION_TTL)])
            tok, s = self.session()
            if not s:
                return self.err(401, "login required")
            if self.headers.get("X-CSRF") != s["csrf"]:
                return self.err(403, "bad CSRF token")
            if post and path == "/api/logout":
                app.sessions.drop(tok)
                return self.send(200, {"ok": True}, extra=[self.cookie("", 0)])
            if post and path == "/api/password":
                if not app.users.check(s["user"], str(data.get("old", ""))):
                    return self.err(403, "current password is wrong")
                try:
                    app.users.set_password(s["user"], str(data.get("new", "")))
                except ValueError as e:
                    return self.err(400, str(e))
                app.sessions.drop_user(s["user"], keep=tok)
                return self.send(200, {"ok": True})
            if not post and path == "/api/config":
                try:
                    cfg = config.validate(copy.deepcopy(data))
                except (config.ConfigError, KeyError, TypeError, ValueError, AttributeError) as e:
                    return self.err(400, f"invalid config: {e}")
                clean = config.dump(cfg)
                with app.lock:
                    app.save_pending(clean)
                return self.send(200, {"pending": clean})
            if post and path == "/api/apply":
                pend, st = app.pending(), app.apply_status().get("state")
                if st in ("applying", "pending-confirm"):
                    return self.err(409, "an apply is already running")
                force = data.get("force") is True           # re-apply the active config, e.g. after a software update
                if force and app.active() and (not pend or pend == app.active()):
                    pend = app.active()
                    app.save_pending(pend)
                elif not pend or pend == app.active():
                    return self.err(409, "nothing to apply: there are no saved changes")
                with app.lock:
                    open(os.path.join(app.dir, "apply.request"), "w").close()
                return self.send(202, {"state": "queued"})
            if post and path == "/api/update-mode":
                if not isinstance(data.get("on"), bool):
                    return self.err(400, "expected {\"on\": true|false}")
                if app.apply_status().get("state") in ("applying", "pending-confirm"):
                    return self.err(409, "an apply is running; wait until it is confirmed")
                with app.lock:
                    with open(os.path.join(app.dir, "update.request"), "w") as f:
                        f.write("on" if data["on"] else "off")
                return self.send(202, {"state": "queued"})
            if post and path == "/api/confirm":
                if app.apply_status().get("state") != "pending-confirm":
                    return self.err(409, "nothing is waiting for confirmation")
                with app.lock:
                    open(os.path.join(app.dir, "confirm"), "w").close()
                return self.send(200, {"ok": True})
            self.err(404, "not found")

    return H


def serve(data_dir, host, port, certfile=None, keyfile=None, etc_dir=None, run_dir=None):
    app = App(data_dir, tls=bool(certfile), etc_dir=etc_dir, run_dir=run_dir)
    srv = http.server.ThreadingHTTPServer((host, port), make_handler(app))
    if certfile:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile, keyfile)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    return app, srv
