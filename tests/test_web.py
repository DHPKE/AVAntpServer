import http.client
import json
import os
import tempfile
import threading
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
