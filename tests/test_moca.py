"""Odczyt adaptera MoCA: odpowiedzi z prawdziwego Kiwee KB-M3-02 (2026-11-18), MAC podmienione."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deco_monitor", "app"))

from incidents import summarize  # noqa: E402
from moca_api import fmt_mac, parse_fmr, parse_frames, parse_local  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
