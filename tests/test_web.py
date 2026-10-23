import http.client
import json
import os
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
            json.dump({"state": state}, open(os.path.join(self.dir, "status.json"), "w"))
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
        m.assert_called_once_with("timecode", "warning", "50", "-1")
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
        json.dump({"state": "pending-confirm"}, open(os.path.join(self.dir, "status.json"), "w"))
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
