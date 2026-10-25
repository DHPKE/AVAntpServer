import http.client
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest

from avantp import web
from tests.test_avantp import BASE


class WebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.app, cls.srv = web.serve(cls.dir, "127.0.0.1", 0)
        cls.app.users.set_password("admin", "correct horse battery")
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def req(self, method, path, body=None, cookie=None, csrf=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        h = {"Content-Type": "application/json"}
        if cookie:
            h["Cookie"] = cookie
        if csrf:
            h["X-CSRF"] = csrf
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        raw = r.read()
        try:
            return r.status, json.loads(raw), r
        except ValueError:
            return r.status, raw, r

    def login(self, pw="correct horse battery"):
        st, d, r = self.req("POST", "/api/login", {"user": "admin", "password": pw})
        if st != 200:
            return st, None, None
        return st, r.getheader("Set-Cookie").split(";")[0], d["csrf"]

    def test_static_and_headers(self):
        st, body, r = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn("default-src 'none'", r.getheader("Content-Security-Policy"))
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")

    def test_api_needs_login(self):
        for p in ("/api/status", "/api/config"):
            self.assertEqual(self.req("GET", p)[0], 401)
        self.assertEqual(self.req("PUT", "/api/config", BASE)[0], 401)

    def test_cookie_flags(self):
        st, d, r = self.req("POST", "/api/login", {"user": "admin", "password": "correct horse battery"})
        sc = r.getheader("Set-Cookie")
        self.assertIn("HttpOnly", sc)
        self.assertIn("SameSite=Strict", sc)

    def test_csrf_required(self):
        st, ck, csrf = self.login()
        self.assertEqual(self.req("PUT", "/api/config", BASE, cookie=ck)[0], 403)
        self.assertEqual(self.req("PUT", "/api/config", BASE, cookie=ck, csrf="wrong")[0], 403)
        self.assertEqual(self.req("PUT", "/api/config", BASE, cookie=ck, csrf=csrf)[0], 200)

    def test_config_roundtrip_and_validation(self):
        st, ck, csrf = self.login()
        st, d, _ = self.req("PUT", "/api/config", BASE, cookie=ck, csrf=csrf)
        self.assertEqual(st, 200)
        st, d, _ = self.req("GET", "/api/config", cookie=ck)
        self.assertEqual(d["pending"]["vlans"][0]["id"], 10)
        self.assertNotIn("_if", json.dumps(d))
        bad = json.loads(json.dumps(BASE))
        bad["vlans"][1]["id"] = 10
        st, d, _ = self.req("PUT", "/api/config", bad, cookie=ck, csrf=csrf)
        self.assertEqual(st, 400)
        st, d, _ = self.req("PUT", "/api/config", {"wan": 5}, cookie=ck, csrf=csrf)
        self.assertEqual(st, 400)

    def test_temp_monitor_settings_roundtrip_and_validation(self):
        st, ck, csrf = self.login()
        c = json.loads(json.dumps(BASE))
        c["tempmon"] = {"enabled": True, "ui_vlan": 10, "sensor_vlan": 20, "sensors": ["10.20.0.101"],
                        "rut240": {"ip": "", "vlan": None}}
        self.assertEqual(self.req("PUT", "/api/config", c, cookie=ck, csrf=csrf)[0], 200)
        st, d, _ = self.req("GET", "/api/config", cookie=ck)
        self.assertEqual(d["pending"]["tempmon"]["sensors"], ["10.20.0.101"])
        self.assertTrue(d["pending"]["tempmon"]["enabled"])
        c["tempmon"]["sensors"] = ["10.99.0.1"]               # not on VLAN 20's network
        self.assertEqual(self.req("PUT", "/api/config", c, cookie=ck, csrf=csrf)[0], 400)
        self.req("PUT", "/api/config", BASE, cookie=ck, csrf=csrf)

    def test_wrong_password_and_rate_limit(self):
        for _ in range(5):
            self.assertEqual(self.login("nope nope nope")[0], 401)
        self.assertEqual(self.login("nope nope nope")[0], 429)
        self.assertEqual(self.login()[0], 429)  # even the right password is refused while throttled
        self.app.sessions.fails.clear()

    def test_password_change_invalidates_other_sessions(self):
        _, ck1, _ = self.login()
        _, ck2, csrf2 = self.login()
        st, d, _ = self.req("POST", "/api/password", {"old": "wrong", "new": "x" * 12}, cookie=ck2, csrf=csrf2)
        self.assertEqual(st, 403)
        st, d, _ = self.req("POST", "/api/password", {"old": "correct horse battery", "new": "short"},
                            cookie=ck2, csrf=csrf2)
        self.assertEqual(st, 400)
        st, d, _ = self.req("POST", "/api/password", {"old": "correct horse battery", "new": "another long pass"},
                            cookie=ck2, csrf=csrf2)
        self.assertEqual(st, 200)
        self.assertEqual(self.req("GET", "/api/config", cookie=ck1)[0], 401)
        self.assertEqual(self.req("GET", "/api/config", cookie=ck2)[0], 200)
        self.app.users.set_password("admin", "correct horse battery")
        self.app.sessions.fails.clear()                   # the wrong old password above counts as a failed attempt

    def test_password_change_is_throttled(self):
        _, ck, csrf = self.login()
        for _ in range(5):
            self.assertEqual(self.req("POST", "/api/password", {"old": "guess", "new": "x" * 12}, cookie=ck, csrf=csrf)[0], 403)
        self.assertEqual(self.req("POST", "/api/password", {"old": "correct horse battery", "new": "x" * 12},
                                  cookie=ck, csrf=csrf)[0], 429)
        self.app.sessions.fails.clear()

    def test_helpers_that_died_do_not_block_forever(self):
        _, ck, csrf = self.login()
        st_path, sw_path = os.path.join(self.dir, "status.json"), os.path.join(self.dir, "software.json")
        json.dump({"state": "applying", "time": time.time() - web.STALE_APPLY - 5}, open(st_path, "w"))
        self.assertEqual(self.req("GET", "/api/apply", cookie=ck)[1]["state"], "error")
        json.dump({"state": "pending-confirm", "time": time.time() - 300, "deadline": time.time() - 120}, open(st_path, "w"))
        self.assertEqual(self.req("GET", "/api/apply", cookie=ck)[1]["state"], "error")
        os.unlink(st_path)
        json.dump({"state": "updating", "time": time.time() - web.STALE_SOFTWARE - 5}, open(sw_path, "w"))
        d = self.req("GET", "/api/software", cookie=ck)[1]
        self.assertEqual(d["state"], "error")
        self.assertIn("stopped without finishing", d["error"])
        self.assertEqual(self.req("POST", "/api/software", {"action": "check"}, cookie=ck, csrf=csrf)[0], 202)
        os.unlink(os.path.join(self.dir, "software.request"))
        os.unlink(sw_path)

    def test_negative_content_length_is_refused(self):
        _, ck, csrf = self.login()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.putrequest("PUT", "/api/config")
        c.putheader("Content-Length", "-1")
        c.putheader("Cookie", ck)
        c.putheader("X-CSRF", csrf)
        c.endheaders()
        self.assertEqual(c.getresponse().status, 400)
        c.close()

    def test_apply_and_confirm_endpoints(self):
        _, ck, csrf = self.login()
        st, d, _ = self.req("POST", "/api/apply", {}, cookie=ck, csrf=csrf)
        self.assertEqual(st, 409)                                   # nothing saved yet differs from active
        self.req("PUT", "/api/config", BASE, cookie=ck, csrf=csrf)
        self.assertEqual(self.req("GET", "/api/apply", cookie=ck)[1]["state"], "idle")
        self.assertEqual(self.req("POST", "/api/apply", {}, cookie=None, csrf=csrf)[0], 401)
        self.assertEqual(self.req("POST", "/api/apply", {}, cookie=ck)[0], 403)   # CSRF
        st, d, _ = self.req("POST", "/api/apply", {}, cookie=ck, csrf=csrf)
        self.assertEqual((st, d["state"]), (202, "queued"))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "apply.request")))
        os.unlink(os.path.join(self.dir, "apply.request"))
        self.assertEqual(self.req("POST", "/api/confirm", {}, cookie=ck, csrf=csrf)[0], 409)   # nothing waiting
        for state, code in (("applying", 409), ("pending-confirm", 409)):
            json.dump({"state": state, "time": time.time(), "deadline": time.time() + 90}, open(os.path.join(self.dir, "status.json"), "w"))
            self.assertEqual(self.req("POST", "/api/apply", {}, cookie=ck, csrf=csrf)[0], code)   # no double apply
        self.assertEqual(self.req("POST", "/api/confirm", {}, cookie=ck, csrf=csrf)[0], 200)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "confirm")))
        os.unlink(os.path.join(self.dir, "confirm"))
        os.unlink(os.path.join(self.dir, "status.json"))

    def test_force_reapply_of_the_active_config(self):
        _, ck, csrf = self.login()
        for f in ("pending.json", "config.json", "status.json", "apply.request"):
            try:
                os.unlink(os.path.join(self.dir, f))
            except FileNotFoundError:
                pass
        self.assertEqual(self.req("POST", "/api/apply", {"force": True}, cookie=ck, csrf=csrf)[0], 409)   # nothing active yet
        self.req("PUT", "/api/config", BASE, cookie=ck, csrf=csrf)
        shutil_copy = open(os.path.join(self.dir, "pending.json")).read()
        open(os.path.join(self.dir, "config.json"), "w").write(shutil_copy)                               # as if applied
        self.assertEqual(self.req("POST", "/api/apply", {}, cookie=ck, csrf=csrf)[0], 409)               # plain apply: nothing new
        self.assertEqual(self.req("POST", "/api/apply", {"force": "yes"}, cookie=ck, csrf=csrf)[0], 409)  # only a real true counts
        st, d, _ = self.req("POST", "/api/apply", {"force": True}, cookie=ck, csrf=csrf)
        self.assertEqual((st, d["state"]), (202, "queued"))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "apply.request")))
        for f in ("pending.json", "config.json", "apply.request"):
            os.unlink(os.path.join(self.dir, f))

    def test_logs_api(self):
        _, ck, csrf = self.login()
        self.assertEqual(self.req("GET", "/api/logs")[0], 401)
        self.assertEqual(self.req("GET", "/api/boots")[0], 401)
        from unittest import mock
        with mock.patch("avantp.web.logs.tail", return_value={"lines": ["x"], "error": None}) as m:
            st, d, _ = self.req("GET", "/api/logs?source=timecode&level=warning&lines=50&boot=-1", cookie=ck)
        self.assertEqual((st, d["lines"]), (200, ["x"]))
        m.assert_called_once_with("timecode", "warning", "50", "-1", "0")
        with mock.patch("avantp.web.logs.tail", return_value={"lines": [], "error": None}) as m:
            self.req("GET", "/api/logs?source=all&since=1792781150.5", cookie=ck)
        self.assertEqual(m.call_args[0][4], "1792781150.5")                                       # CLEAR is passed on, validated in logs
        self.assertEqual(self.req("GET", "/api/logs?since=notanumber", cookie=ck)[0], 400)
        with mock.patch("avantp.web.logs.tail", side_effect=ValueError("bad")):
            self.assertEqual(self.req("GET", "/api/logs?source=--vacuum-size=1", cookie=ck)[0], 400)
        with mock.patch("avantp.web.logs.boots", return_value={"boots": [], "error": None}):
            self.assertEqual(self.req("GET", "/api/boots", cookie=ck)[1], {"boots": [], "error": None})

    def test_a_bug_in_one_request_gives_a_500_not_a_dead_connection(self):
        _, ck, csrf = self.login()
        from unittest import mock
        with mock.patch.object(web.App, "update_status", side_effect=RuntimeError("boom")):
            st, d, _ = self.req("GET", "/api/update-mode", cookie=ck)
        self.assertEqual(st, 500)
        self.assertIn("Log tab", d["error"])
        self.assertEqual(self.req("GET", "/api/update-mode", cookie=ck)[0], 200)             # the service carries on

    def test_timecode_status_endpoint(self):
        _, ck, csrf = self.login()
        self.assertEqual(self.req("GET", "/api/timecode")[0], 401)
        path = self.app.tc_status
        if os.path.exists(path):
            os.unlink(path)
        d = self.req("GET", "/api/timecode", cookie=ck)[1]
        self.assertEqual((d["running"], d["vlans"]), (False, []))                         # no service, no file
        json.dump({"time": time.time(), "vlans": [{"id": 20, "artnet": {"state": "sending"}}]}, open(path, "w"))
        d = self.req("GET", "/api/timecode", cookie=ck)[1]
        self.assertTrue(d["running"])
        self.assertEqual(d["vlans"][0]["artnet"]["state"], "sending")
        json.dump({"time": time.time() - 30, "vlans": []}, open(path, "w"))
        self.assertFalse(self.req("GET", "/api/timecode", cookie=ck)[1]["running"])      # stale file = service is dead
        open(path, "w").write("garbage")
        self.assertFalse(self.req("GET", "/api/timecode", cookie=ck)[1]["running"])
        os.unlink(path)

    def test_software_endpoint(self):
        _, ck, csrf = self.login()
        self.assertEqual(self.req("GET", "/api/software")[0], 401)
        self.assertEqual(self.req("POST", "/api/software", {"action": "update"})[0], 401)
        self.assertEqual(self.req("POST", "/api/software", {"action": "update"}, cookie=ck)[0], 403)         # CSRF
        for bad in ({}, {"action": "reboot"}, {"action": "UPDATE"}, {"action": ["update"]}, {"action": "update; id"}):
            self.assertEqual(self.req("POST", "/api/software", bad, cookie=ck, csrf=csrf)[0], 400, bad)
        st = self.req("GET", "/api/software", cookie=ck)[1]
        self.assertEqual(st["state"], "idle")
        self.assertIn("installed_now", st)
        req = os.path.join(self.dir, "software.request")
        for word in ("check", "update"):
            st, d, _ = self.req("POST", "/api/software", {"action": word}, cookie=ck, csrf=csrf)
            self.assertEqual((st, d["state"]), (202, "queued"))
            self.assertEqual(open(req).read(), word)                                                       # nothing but the word
            os.unlink(req)
        path = os.path.join(self.dir, "software.json")
        for busy in ("checking", "updating"):
            json.dump({"state": busy, "time": time.time()}, open(path, "w"))
            self.assertEqual(self.req("POST", "/api/software", {"action": "update"}, cookie=ck, csrf=csrf)[0], 409)
        json.dump({"state": "done"}, open(path, "w"))
        self.assertEqual(self.req("POST", "/api/software", {"action": "check"}, cookie=ck, csrf=csrf)[0], 202)
        os.unlink(req)
        json.dump({"state": "pending-confirm", "deadline": time.time() + 90}, open(os.path.join(self.dir, "status.json"), "w"))
        self.assertEqual(self.req("POST", "/api/software", {"action": "update"}, cookie=ck, csrf=csrf)[0], 409)
        os.unlink(os.path.join(self.dir, "status.json"))
        os.unlink(path)

    def test_update_mode_endpoint(self):
        _, ck, csrf = self.login()
        self.assertEqual(self.req("POST", "/api/update-mode", {"on": True})[0], 401)
        self.assertEqual(self.req("POST", "/api/update-mode", {"on": True}, cookie=ck)[0], 403)          # CSRF
        for bad in ({}, {"on": "yes"}, {"on": 1}, {"on": None}, {"duration": 99999}):
            self.assertEqual(self.req("POST", "/api/update-mode", bad, cookie=ck, csrf=csrf)[0], 400, bad)
        st, d, _ = self.req("POST", "/api/update-mode", {"on": True}, cookie=ck, csrf=csrf)
        self.assertEqual((st, d["state"]), (202, "queued"))
        req = os.path.join(self.dir, "update.request")
        self.assertEqual(open(req).read(), "on")                      # the helper reads nothing but this word
        self.req("POST", "/api/update-mode", {"on": False}, cookie=ck, csrf=csrf)
        self.assertEqual(open(req).read(), "off")
        os.unlink(req)
        self.assertEqual(self.req("GET", "/api/update-mode", cookie=ck)[1]["state"], "off")
        self.assertEqual(self.req("GET", "/api/update-mode")[0], 401)
        # the status expires on its own when the kernel timeout has passed
        json.dump({"state": "on", "until": time.time() + 600}, open(os.path.join(self.dir, "update.json"), "w"))
        self.assertEqual(self.req("GET", "/api/update-mode", cookie=ck)[1]["state"], "on")
        json.dump({"state": "on", "until": time.time() - 5}, open(os.path.join(self.dir, "update.json"), "w"))
        self.assertEqual(self.req("GET", "/api/update-mode", cookie=ck)[1]["state"], "off")
        os.unlink(os.path.join(self.dir, "update.json"))
        # not while an apply is waiting for confirmation
        json.dump({"state": "pending-confirm", "deadline": time.time() + 90}, open(os.path.join(self.dir, "status.json"), "w"))
        self.assertEqual(self.req("POST", "/api/update-mode", {"on": True}, cookie=ck, csrf=csrf)[0], 409)
        os.unlink(os.path.join(self.dir, "status.json"))

    def test_version_is_reported_after_login(self):
        st, d, _ = self.req("POST", "/api/login", {"user": "admin", "password": "correct horse battery"})
        self.assertIn("version", d)
        _, ck, csrf = self.login()
        st, d, _ = self.req("GET", "/api/session", cookie=ck)
        self.assertEqual(d["version"], web.version())
        self.assertNotIn("version", self.req("GET", "/api/session")[1])     # nothing leaks before login

    def test_logout(self):
        _, ck, csrf = self.login()
        self.assertEqual(self.req("POST", "/api/logout", {}, cookie=ck, csrf=csrf)[0], 200)
        self.assertEqual(self.req("GET", "/api/config", cookie=ck)[0], 401)

    def test_oversize_body(self):
        _, ck, csrf = self.login()
        st, d, _ = self.req("PUT", "/api/config", {"x": "a" * 300000}, cookie=ck, csrf=csrf)
        self.assertEqual(st, 400)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(shutil.which("openssl"), "needs openssl for a test certificate")
class TlsTest(unittest.TestCase):
    """Over TLS the handshake must not run in the accepting thread: one silent client would freeze the WebUI."""

    def test_idle_connection_does_not_block_others(self):
        d = tempfile.mkdtemp()
        crt, key = os.path.join(d, "tls.crt"), os.path.join(d, "tls.key")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=t",
                        "-keyout", key, "-out", crt], capture_output=True, check=True)
        _, srv = web.serve(d, "127.0.0.1", 0, crt, key)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        idle = socket.create_connection(("127.0.0.1", port))             # connects, never starts TLS
        try:
            ctx = ssl._create_unverified_context()
            c = http.client.HTTPSConnection("127.0.0.1", port, timeout=5, context=ctx)
            c.request("GET", "/api/session")
            self.assertEqual(c.getresponse().status, 200)
            c.close()
        finally:
            idle.close()
            srv.shutdown()
