"""WebUI backend: stdlib only. Serves ui/, a JSON API behind a login, optionally over TLS.

Config flow: the browser edits a config, PUT /api/config validates it and stores it as pending.json.
POST /api/apply asks the separate root helper (avantp apply) to put it on the system; the helper reports in status.json
and rolls back by itself unless POST /api/confirm arrives in time. This process never needs root.
"""
import copy
import http.server
import json
import logging
import os
import ssl
import threading
import time
import traceback
from http import cookies
from urllib.parse import parse_qs, urlsplit

from . import auth, config, logs, status

log = logging.getLogger("web")
UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript"),
          "/vue.js": ("vue.global.prod.js", "text/javascript")}
CSP = ("default-src 'none'; script-src 'self' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; "
       "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
MAX_BODY = 256 * 1024
CONN_TIMEOUT = 20                     # seconds a connection may sit idle (TLS handshake, request line, body)
STALE_SOFTWARE, STALE_APPLY = 1300, 600


def version():
    try:
        with open(os.path.join(os.path.dirname(UI_DIR), "VERSION")) as f:
            return f.read().strip()[:40] or "unknown"
    except OSError:
        return "unknown"


class App:
    def __init__(self, data_dir, tls, etc_dir=None, run_dir=None, tc_status=None):
        self.dir = data_dir
        self.etc = etc_dir or data_dir   # root-owned: the active config
        self.run = run_dir or data_dir   # root-owned: the helper's status
        self.tls = tls
        self.tc_status = tc_status or os.path.join(data_dir, "timecode-status.json")
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

    def timecode_status(self):
        """What the timecode service reports; it counts as running only if it wrote the file in the last 5 seconds."""
        try:
            with open(self.tc_status) as f:
                st = json.load(f)
        except (OSError, ValueError):
            return {"running": False, "vlans": []}
        st["running"] = time.time() - st.get("time", 0) < 5
        return st

    def software_status(self):
        st = self._read("software.json", self.run) or {"state": "idle"}
        # the helper rewrites the file on every step; systemd kills it after 1200 s. Silence for longer means it died (killed,
        # crashed, power cut mid-update) and the state would otherwise block every later check or update
        if st.get("state") in ("checking", "updating") and time.time() - st.get("time", 0) > STALE_SOFTWARE:
            st = dict(st, state="error", error="the update helper stopped without finishing (see the Log tab, source "
                      "'Software update'); you can start a new check or update")
        st["installed_now"] = version()                      # what this process runs (changes after an update + restart)
        return st

    def update_status(self):
        st = self._read("update.json", self.run) or {"state": "off"}
        if st.get("state") == "on" and st.get("until", 0) < time.time():   # the kernel closed it
            st = {"state": "off", "time": st.get("until")}
        return st

    def apply_status(self):
        st = self._read("status.json", self.run) or {"state": "idle"}
        now = time.time()
        # the same for a helper that died mid-apply: without this, APPLY and UPDATE stay refused until the next reboot
        if (st.get("state") == "pending-confirm" and now > st.get("deadline", 0) + 60 or
                st.get("state") == "applying" and now - st.get("time", 0) > STALE_APPLY):
            st = {"state": "error", "time": st.get("time"), "error": "the apply helper stopped without finishing (see the Log "
                  "tab). The previous setup is restored at the next restart of the box; or apply again."}
        return st

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
        timeout = CONN_TIMEOUT

        def handle(self):
            # the TLS handshake runs here, in this connection's own thread and with a time limit (setup() set it). Done in
            # accept() (Python's default), one client that connects and sends nothing would freeze the WebUI for everybody.
            try:
                if isinstance(self.request, ssl.SSLSocket):
                    self.request.do_handshake()
                super().handle()
            except (ssl.SSLError, OSError):     # handshake failed or timed out, client went away: just drop the connection
                pass

        def log_message(self, fmt, *a):
            log.info("%s %s", self.client_address[0], fmt % a)

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
            if n < 0 or n > MAX_BODY:          # -1 would mean "read until the client closes"
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

        # ---- routing: an unexpected error is written to the journal with its traceback and answered with a clean 500,
        # so a bug in one request can neither kill the service silently nor leave the browser hanging
        def guarded(self, fn):
            try:
                fn()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                log.error("unhandled exception in %s %s from %s\n%s", self.command, self.path.split("?")[0],
                          self.client_address[0], traceback.format_exc().rstrip())
                try:
                    self.err(500, "internal error: see the Log tab")
                except Exception:
                    pass

        def do_GET(self):
            self.guarded(self._get)

        def _get(self):
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
            if path == "/api/timecode":
                return self.send(200, app.timecode_status())
            if path == "/api/software":
                return self.send(200, app.software_status())
            if path == "/api/logs":
                q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
                try:
                    return self.send(200, logs.tail(q.get("source", "all"), q.get("level", "all"),
                                                     q.get("lines", "200"), q.get("boot", "0"), q.get("since", "0")))
                except (ValueError, TypeError):
                    return self.err(400, "bad log request")
            if path == "/api/boots":
                return self.send(200, logs.boots())
            self.err(404, "not found")

        def do_POST(self):
            self.guarded(lambda: self.mutate(post=True))

        def do_PUT(self):
            self.guarded(lambda: self.mutate(post=False))

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
                    log.warning("login failed for %r from %s", u[:32], ip)
                    return self.err(401, "wrong user or password")
                log.info("login ok: %s from %s", u, ip)
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
                if app.sessions.throttled(ip):          # same brake as the login: an open session must not allow guessing
                    return self.err(429, "too many wrong passwords, wait a few minutes")
                if not app.users.check(s["user"], str(data.get("old", ""))):
                    app.sessions.fail(ip)
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
            if post and path == "/api/software":
                if data.get("action") not in ("check", "update"):
                    return self.err(400, "expected {\"action\": \"check\" or \"update\"}")
                if app.software_status().get("state") in ("checking", "updating"):
                    return self.err(409, "a check or update is already running")
                if app.apply_status().get("state") in ("applying", "pending-confirm"):
                    return self.err(409, "an apply is waiting for confirmation; finish that first")
                with app.lock:
                    with open(os.path.join(app.dir, "software.request"), "w") as f:
                        f.write(data["action"])
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


def serve(data_dir, host, port, certfile=None, keyfile=None, etc_dir=None, run_dir=None, tc_status=None):
    app = App(data_dir, tls=bool(certfile), etc_dir=etc_dir, run_dir=run_dir, tc_status=tc_status)
    srv = http.server.ThreadingHTTPServer((host, port), make_handler(app))
    if certfile:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile, keyfile)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    return app, srv
