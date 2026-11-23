"""Odczyt adaptera MoCA: odpowiedzi z prawdziwego Kiwee KB-M3-02 (2026-11-18), MAC podmienione."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deco_monitor", "app"))

import main  # noqa: E402
from incidents import summarize  # noqa: E402
from moca_api import MocaUnreachable, fmt_mac, parse_fmr, parse_frames, parse_local  # noqa: E402

LOCAL = [0x0, 0x0, 0x1, 0x1, 0x0, 0x1, 0x0, 0x0, 0x1, 0x8, 0x0, 0x25, 0x3, 0x0, 0x0, 0x0, 0xfbfffe, 0x29,
         0x1, 0x2, 0x0, 0x312e3138, 0x2e313300, 0x0, 0x0, 0x0, 0x0, 0x0, 0x1]
FMR0 = [0x0, 0x1, 0x19ff00] + [0xff00ff00] * 7 + [0xc0c11f8, 0x11f80c00, 0x5dad0000, 0x0, 0x0, 0x0]
FMR1 = [0x0, 0x2, 0x119ff00] + [0xff00ff00] * 7 + [0xc005dae, 0xc0c, 0x11f811f8, 0x0, 0x0, 0x0]


class MocaParseTest(unittest.TestCase):
    def test_local(self):
        loc = parse_local(LOCAL)
        self.assertEqual(loc["node_id"], 0)
        self.assertEqual(loc["nc_id"], 0)
        self.assertTrue(loc["link_up"])
        self.assertEqual(loc["net_version"], "2.5")
        self.assertEqual(loc["node_mask"], 3)

    def test_mac(self):
        self.assertEqual(fmt_mac(0x02d2b675, 0x1b9d0000), "02:d2:b6:75:1b:9d")

    def test_phy_rates_like_panel(self):
        vers = {0: 0x25, 1: 0x25}
        # zgodnie z phyRates.html: 0x5dad i 0x5dae bitów na symbol przy CP 12 -> 3656 Mb/s
        self.assertEqual(parse_fmr(FMR0, 3, 0x25, 0x25, vers)[1][0], 3656)
        self.assertEqual(parse_fmr(FMR1, 3, 0x25, 0x25, vers)[0][0], 3656)

    def test_frames(self):
        vals = [0] * 108
        vals[13], vals[31], vals[49] = 590408, 2, 0
        vals[66], vals[67], vals[85], vals[103] = 1, 5, 0, 7
        fr = parse_frames(vals)
        self.assertEqual(fr["tx_good"], 590408)
        self.assertEqual(fr["tx_bad"], 2)
        self.assertEqual(fr["rx_good"], 4294967296 + 5)
        self.assertEqual(fr["rx_dropped"], 7)

    def test_incident_cause(self):
        inc = {"items": [
            {"ts": 1000.0, "type": "moca_link_down", "text": "MoCA: łącze zerwane", "severity": "problem", "data": {}},
            {"ts": 1002.0, "type": "ping_down", "text": "Ping living_room", "severity": "problem",
             "data": {"name": "Satelita living_room", "kind": "mesh"}},
        ]}
        self.assertTrue(summarize(inc)["cause"].startswith("Zanik łącza MoCA"))


def frames(rx_bad=0, good=0):
    return {"tx_good": good, "tx_bad": 0, "tx_dropped": 0, "rx_good": good, "rx_bad": rx_bad, "rx_dropped": 0}


class FakeAdapter:
    def __init__(self, node_id, mac):
        self.node_id, self.mac, self.fr, self.down = node_id, mac, frames(), False

    def read_status(self):
        if self.down:
            raise MocaUnreachable("brak")
        return {"node_id": self.node_id, "nc_id": 0, "link_up": True, "net_version": "2.5", "node_mask": 3,
                "mac": self.mac, "nodes": {0: {"mac": "02:00:00:00:1b:9d", "ver": 0x25},
                                           1: {"mac": "02:00:00:00:1c:62", "ver": 0x25}},
                "frames": dict(self.fr), "lof_mhz": 1150, "channels": 5}

    def read_phy(self, st):
        return {}


class MocaTwoAdaptersTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        os.environ.update(DECO_MONITOR_DATA=os.path.join(tmp, "data"), DECO_MONITOR_SHARE=os.path.join(tmp, "share"),
                          DECO_MONITOR_DRY_RUN="1")
        os.environ.pop("SUPERVISOR_TOKEN", None)
        self.mon = main.Monitor({"deco_host": "127.0.0.1", "deco_password": "x", "ping_targets": [],
                                 "ping_mesh_nodes": False, "moca_host": " 192.168.144.200, 192.168.144.201,",
                                 "moca_names": ["02:00:00:00:1b:9d=poddasze", "02:00:00:00:1c:62=salon"]})
        self.a = FakeAdapter(0, "02:00:00:00:1b:9d")
        self.b = FakeAdapter(1, "02:00:00:00:1c:62")
        self.mon.moca, self.mon.moca_extra = self.a, [self.b]
        self.events = []
        self.mon.emit = lambda etype, msg, *a, **k: self.events.append((etype, msg))

    def test_hosts(self):
        self.assertEqual(self.mon.moca_hosts, ["192.168.144.200", "192.168.144.201"])
        self.assertEqual(main.parse_hosts("x; y, x"), ["x", "y"])
        self.assertEqual(main.parse_hosts(""), [])

    def test_counters_from_both_and_throttled_event(self):
        t = [1000.0]
        real = main.time

        class Clock:  # tylko zegar modułu main, bez podmiany globalnego time.time
            def __getattr__(self, name):
                return getattr(real, name)

            @staticmethod
            def time():
                return t[0]

        main.time = Clock()
        try:
            self.mon.poll_moca()
            for k in ("moca_1b9d_bad", "moca_1c62_bad", "moca_1c62_rate", "ping_moca", "ping_moca_1c62"):
                self.assertIn(k, self.mon.ents)
            self.assertEqual(self.mon.ping_meta["moca_1c62"]["host"], "192.168.144.201")
            self.assertEqual(self.mon.ping_meta["moca_1c62"]["name"], "Adapter MoCA salon")
            for i, bad in enumerate((67, 134, 201)):  # co minutę +67
                t[0] += 60
                self.b.fr = frames(rx_bad=bad, good=1000 * (i + 1))
                self.mon.poll_moca()
            errs = [m for e, m in self.events if e == "moca_eth_errors"]
            self.assertEqual(len(errs), 1)
            self.assertIn("salon: błędne ramki +67", errs[0])
            self.assertEqual(self.mon.values["moca_1c62_bad"], 201)
            t[0] += main.MOCA_ERR_EVENT_EVERY_S
            self.b.fr = frames(rx_bad=210, good=5000)
            self.mon.poll_moca()
            errs = [m for e, m in self.events if e == "moca_eth_errors"]
            self.assertEqual(len(errs), 2)
            self.assertIn("w 17 min błędne ramki +143", errs[1])  # +67 +67 po pierwszym zdarzeniu (t=1060) i +9 (t=2080)
            self.b.down = True  # drugi adapter milczy: pierwszy dalej działa
            self.mon.poll_moca()
            self.mon.poll_moca()
            self.assertTrue(self.mon.values["moca_link"])
            self.assertIn("błąd", self.mon.status["moca 192.168.144.201"])
        finally:
            main.time = real


if __name__ == "__main__":
    unittest.main()
