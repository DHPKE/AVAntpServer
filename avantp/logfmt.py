"""Logging to the systemd journal with the right priority.

systemd stores everything a service prints to stderr as priority "info". A line that starts with "<N>" (N = syslog
level, see sd-daemon(3)) is stored with that priority instead, so `journalctl -p warning` and the WebUI's level filter
see real warnings and errors. Only done under systemd (INVOCATION_ID is set there); in a terminal the output stays plain.
"""
import logging
import os
import sys

LEVELS = {logging.CRITICAL: 2, logging.ERROR: 3, logging.WARNING: 4, logging.INFO: 6, logging.DEBUG: 7}


class JournalFormatter(logging.Formatter):
    def format(self, record):
        text = super().format(record)
        prio = LEVELS.get(record.levelno, 6)
        return "\n".join(f"<{prio}>{line}" for line in text.splitlines() or [""])    # every line, so a traceback is an error too


def setup(stream=None):
    h = logging.StreamHandler(stream or sys.stderr)
    fmt = "%(name)s: %(levelname)s %(message)s"
    h.setFormatter(JournalFormatter(fmt) if os.environ.get("INVOCATION_ID") else logging.Formatter(fmt))
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(logging.INFO)
