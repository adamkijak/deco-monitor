"""Logi satelit: python3 -m unittest discover tests"""

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deco_monitor", "app"))

from deco_api import DecoClient, DecoError  # noqa: E402
from satlogs import BLOCKED_RETRY, SatelliteLogs  # noqa: E402
from store import Store, valid_source  # noqa: E402

NODES = [
    {"name": "Poddasze", "ip": "192.168.68.1", "master": True},
    {"name": "kitchen", "ip": "192.168.71.247", "master": False},
    {"name": "hallway", "ip": "192.168.71.248", "master": False},
]


def L(sec, msg):
    return f"Fri Nov  6 20:{sec // 60:02d}:{sec % 60:02d} 2026 daemon.notice nrd[4479]: {msg}"


class FakeDeco:
    """M4R: logowanie tylko po HTTP; M3W: 403 na wszystkim; inne: brak połączenia."""

    buffers: dict = {}
    logins: list = []

    def __init__(self, host, password, scheme="https", legacy_tls=False):
        self.host, self.scheme = host, scheme

    def login(self):
        FakeDeco.logins.append((self.host, self.scheme))
        if self.host == "192.168.71.247" and self.scheme == "http":
            return
        if self.host == "192.168.71.247":
            raise DecoError("Brak połączenia z Deco: [SSL: UNSUPPORTED_PROTOCOL]")
        if self.host == "192.168.71.248":
            raise DecoError("HTTP 403 login")
        raise DecoError("Brak połączenia z Deco: timed out")

    def fetch_syslog(self):
        return "\n".join(FakeDeco.buffers.get(self.host, [])) + "\n"


class SatelliteLogsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(self.dir, 30)
        self.now = 1_000_000.0
        FakeDeco.buffers, FakeDeco.logins = {}, []

    def tearDown(self):
        shutil.rmtree(self.dir)

    def sat(self):
        s = SatelliteLogs(self.store, "pw", 300, client_factory=FakeDeco, clock=lambda: self.now)
        s.update_nodes(NODES)
        return s

    def test_kitchen_archived_separately_and_deduped(self):
        FakeDeco.buffers["192.168.71.247"] = [L(i, f"m{i}") for i in range(5)]
        s = self.sat()
        s.poll()
        self.assertEqual(self.store.sources(), ["kitchen"])
        self.assertEqual(self.store.days("kitchen"), ["2026-11-06"])
        self.assertEqual(self.store.days(), [])  # log głównego Deco nietknięty
        self.assertTrue(s.status()["kitchen"].startswith("OK (HTTP"))
        FakeDeco.buffers["192.168.71.247"] += [L(10, "nowa")]
        s.poll()
        lines = self.store.read_log("2026-11-06", source="kitchen")["lines"]
        self.assertEqual(lines, [L(i, f"m{i}") for i in range(5)] + [L(10, "nowa")])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "logs", "deco-kitchen-2026-11-06.log")))

    def test_rejecting_satellite_retried_after_6h(self):
        s = self.sat()
        s.poll()
        self.assertIn("brak dostępu", s.status()["hallway"])
        tries = [l for l in FakeDeco.logins if l[0] == "192.168.71.248"]
        self.assertEqual(tries, [("192.168.71.248", "http"), ("192.168.71.248", "https")])
        self.now += 3600
        s.poll()
        self.assertEqual(len([l for l in FakeDeco.logins if l[0] == "192.168.71.248"]), 2)
        self.now += BLOCKED_RETRY
        s.poll()
        self.assertEqual(len([l for l in FakeDeco.logins if l[0] == "192.168.71.248"]), 4)

    def test_restart_remembers_satellite_archive(self):
        FakeDeco.buffers["192.168.71.247"] = [L(i, f"m{i}") for i in range(5)]
        self.sat().poll()
        FakeDeco.buffers["192.168.71.247"] += [L(10, "po restarcie")]
        self.sat().poll()  # nowa instancja = restart dodatku
        lines = self.store.read_log("2026-11-06", source="kitchen")["lines"]
        self.assertEqual(len(lines), 6)

    def test_satellite_retention_shorter(self):
        st = Store(self.dir, 30, 7)
        st.append_log([(None, "x")])
        for name in ("deco-2000-01-01.log", "deco-kitchen-2000-01-01.log"):
            open(os.path.join(self.dir, "logs", name), "w").write("stary\n")
        recent = time.strftime("%Y-%m-%d", time.localtime(time.time() - 10 * 86400))
        for name in (f"deco-{recent}.log", f"deco-kitchen-{recent}.log"):
            open(os.path.join(self.dir, "logs", name), "w").write("10 dni\n")
        st.cleanup()
        self.assertEqual(st.days("kitchen"), [])  # satelita: 7 dni
        self.assertIn(recent, st.days())  # główny Deco: 30 dni
        self.assertNotIn("2000-01-01", st.days())

    def test_master_is_not_polled(self):
        self.sat().poll()
        self.assertFalse([l for l in FakeDeco.logins if l[0] == "192.168.68.1"])


class SourceValidationTest(unittest.TestCase):
    def test_source_names(self):
        self.assertTrue(valid_source(""))
        self.assertTrue(valid_source("living_room"))
        for bad in ("../etc", "a/b", "Kitchen", "a.b", "x" * 41):
            self.assertFalse(valid_source(bad), bad)

    def test_bad_source_reads_nothing(self):
        d = tempfile.mkdtemp()
        try:
            st = Store(d, 30)
            self.assertEqual(st.read_log("2026-11-06", source="../x")["lines"], [])
            self.assertEqual(st.read_log("../../x", source="kitchen")["lines"], [])
            with self.assertRaises(ValueError):
                st.log_path("2026-11-06", "../x")
        finally:
            shutil.rmtree(d)

    def test_client_scheme(self):
        self.assertTrue(DecoClient("h", "p", scheme="http")._login_url("auth").startswith("http://h/"))
        self.assertTrue(DecoClient("h", "p")._login_url("auth").startswith("https://h/"))


if __name__ == "__main__":
    unittest.main()
