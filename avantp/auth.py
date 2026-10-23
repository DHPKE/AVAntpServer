"""Users, password hashing (scrypt, stdlib), sessions and login rate limiting."""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

SCRYPT = dict(n=2 ** 14, r=8, p=1)
MIN_PASSWORD = 10
SESSION_TTL = 8 * 3600
FAIL_LIMIT, FAIL_WINDOW = 5, 300


def hash_password(pw):
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, dklen=32, **SCRYPT)
    return f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${salt.hex()}${h.hex()}"


def verify_password(pw, stored):
    try:
        _, n, r, p, salt, h = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), dklen=32, n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(calc.hex(), h)
    except (ValueError, TypeError):
        return False


class Users:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def _load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _save(self, d):
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, self.path)

    def set_password(self, user, pw):
        if len(pw) < MIN_PASSWORD:
            raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
        with self.lock:
            d = self._load()
            d[user] = hash_password(pw)
            self._save(d)

    def check(self, user, pw):
        stored = self._load().get(user)
        # always do one scrypt run so unknown users cost the same as wrong passwords
        return verify_password(pw, stored or hash_password("x")) and stored is not None

    def any(self):
        return bool(self._load())


class Sessions:
    def __init__(self):
        self.lock = threading.Lock()
        self.s = {}
        self.fails = {}

    def throttled(self, ip):
        with self.lock:
            now = time.time()
            self.fails[ip] = [t for t in self.fails.get(ip, []) if now - t < FAIL_WINDOW]
            return len(self.fails[ip]) >= FAIL_LIMIT

    def fail(self, ip):
        with self.lock:
            self.fails.setdefault(ip, []).append(time.time())

    def create(self, user):
        tok, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.lock:
            self.s[tok] = {"user": user, "csrf": csrf, "exp": time.time() + SESSION_TTL}
        return tok, csrf

    def get(self, tok):
        with self.lock:
            s = self.s.get(tok)
            if s and s["exp"] > time.time():
                return s
            self.s.pop(tok, None)
            return None

    def drop(self, tok):
        with self.lock:
            self.s.pop(tok, None)

    def drop_user(self, user, keep=None):
        with self.lock:
            for t in [t for t, s in self.s.items() if s["user"] == user and t != keep]:
                del self.s[t]
