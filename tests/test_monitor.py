"""python3 -m unittest discover tests

Linie logu są wzorowane na prawdziwym syslogu Deco X50-4G (zanik WAN), z podmienionymi
adresami MAC i IP.
"""

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deco_monitor", "app"))

from incidents import summarize  # noqa: E402
from mqtt_pub import slugify  # noqa: E402
from switch_api import _parse_all_info  # noqa: E402
from syslog_parser import LineDiffer, LogAnalyzer, mac_tail, parse_line  # noqa: E402

NODES = [
    {"name": "Poddasze", "ip": "192.168.68.1", "macs": ["02-00-00-00-01-08"], "master": True},
    {"name": "bedroom", "ip": "192.168.71.249", "macs": ["B8-00-00-AA-DE-28", "B8:00:00:AA:DE:2B"], "wired": True},
    {"name": "kitchen", "ip": "192.168.71.247", "macs": ["00-31-00-29-CA-B4", "00:31:00:29:CA:B7"], "wired": False},
]

LOG = """\
Tue Nov  3 17:09:33 2026 daemon.err /usr/bin/apsd: apsd_get_active_ports:234: Error: No such interface 41
Tue Nov  3 17:09:34 2026 daemon.notice netifd: Network device 'br-wan' link is down
Tue Nov  3 17:09:34 2026 daemon.notice netifd: Interface 'wan' has link connectivity loss
Tue Nov  3 17:09:36 2026 daemon.notice conn-indicator: Connectivity changed: ONLINE(0x0) -> OFFLINE(0x1)
Tue Nov  3 17:09:37 2026 user.notice smartip: [lan] kill process udhcpd
Tue Nov  3 17:09:46 2026 user.err : Error: ssh_connect: Timeout connecting to 192.168.71.249
Tue Nov  3 17:09:46 2026 user.err : Error: ssh_connect: Timeout connecting to 192.168.71.249
Tue Nov  3 17:09:49 2026 daemon.notice netifd: Network device 'br-wan' link is up
Tue Nov  3 17:09:49 2026 daemon.notice hostapd: ath12: AP-STA-CONNECTED c6:00:00:aa:de:2b
Tue Nov  3 17:09:51 2026 daemon.notice netifd: wan (25035): Lease of 203.0.113.25 obtained, lease time 28800
Tue Nov  3 17:09:52 2026 daemon.notice conn-indicator: Connectivity changed: OFFLINE(0x1) -> ONLINE(0x0)
Tue Nov  3 17:09:52 2026 user.notice mobile_backup: ewan_recovered
Tue Nov  3 17:10:05 2026 daemon.notice hostapd: ath12: AP-STA-DISCONNECTED c6:00:00:aa:de:2b
Tue Nov  3 17:10:06 2026 daemon.notice hostapd: ath1: AP-STA-CONNECTED 3c:00:00:99:6a:23
"""

KICK = """\
Tue Nov  3 17:01:47 2026 user.notice smartip: [lan] disconnect wifi(lan) wifi client!!
Tue Nov  3 17:01:52 2026 user.notice smartip: [lan] ##### disconnect eth port 1
Tue Nov  3 17:01:53 2026 user.notice smartip: [lan] ##### disconnect eth port 0
Tue Nov  3 17:02:01 2026 user.notice mobile_backup: ewan_disconnect
Tue Nov  3 17:02:23 2026 user.notice mobile_backup: ewan_recovered
"""


def analyze(text):
    a = LogAnalyzer()
    a.update_nodes(NODES)
    out = []
    for line in text.splitlines():
        out += [e for e in a.analyze(parse_line(line)) if not e.type.startswith("_")]
    return out


class ParserTest(unittest.TestCase):
    def test_parse_line(self):
        ln = parse_line("Tue Nov  3 17:09:34 2026 daemon.notice netifd: Network device 'br-wan' link is down")
        self.assertEqual(ln.time, datetime(2026, 11, 3, 17, 9, 34))
        self.assertEqual((ln.facility, ln.level), ("daemon", "notice"))
        self.assertTrue(ln.msg.startswith("netifd:"))
        self.assertIsNone(parse_line("śmieci").time)

    def test_wan_outage_sequence(self):
        types = [e.type for e in analyze(LOG)]
        self.assertEqual(types, [
            "wan_link_down", "internet_offline", "lan_dhcp_restart", "mesh_node_unreachable",
            "wan_link_up", "backhaul_radio_start", "wan_lease", "internet_online", "backhaul_radio_end",
        ])  # bez failover_to_wan: łącze zapasowe nie było aktywne, drugi timeout ssh zdeduplikowany

    def test_states_and_names(self):
        ev = {e.type: e for e in analyze(LOG)}
        self.assertEqual(ev["wan_link_down"].states, {"wan_link": False})
        self.assertEqual(ev["wan_lease"].states, {"wan_ip": "203.0.113.25"})
        self.assertIn("bedroom", ev["mesh_node_unreachable"].message)
        start = ev["backhaul_radio_start"]
        self.assertEqual(start.severity, "problem")  # satelita na kablu przeszła na radio
        self.assertEqual(start.states, {"radio_backhaul:bedroom": True})

    def test_kick_and_failover(self):
        types = [e.type for e in analyze(KICK)]
        self.assertEqual(types, ["clients_kicked", "eth_ports_reset", "failover_to_mobile", "failover_to_wan"])

    def test_mac_tail(self):
        self.assertEqual(mac_tail("C6:00:00:AA:DE:2B"), mac_tail("b8-00-00-aa-de-2b"))
        self.assertEqual(mac_tail("zły"), "")


class DifferTest(unittest.TestCase):
    def test_new_lines_after_rotation(self):
        d = LineDiffer()
        first = [f"l{i}" for i in range(10)]
        self.assertEqual(d.new_lines(first), first)
        self.assertEqual(d.new_lines([f"l{i}" for i in range(3, 13)]), ["l10", "l11", "l12"])
        self.assertEqual(d.new_lines([f"l{i}" for i in range(3, 13)]), [])
        # bufor przewinął się w całości - wszystko jest nowe
        self.assertEqual(d.new_lines(["x1", "x2"]), ["x1", "x2"])

    def test_repeated_identical_lines(self):
        d = LineDiffer()
        d.new_lines(["a", "b", "b", "b", "b", "b"])
        self.assertEqual(d.new_lines(["a", "b", "b", "b", "b", "b", "b", "c"]), ["b", "c"])


class IncidentTest(unittest.TestCase):
    def test_summary(self):
        t0 = datetime(2026, 11, 3, 17, 9, 34).timestamp()
        inc = summarize({"items": [
            {"ts": t0, "type": "wan_link_down", "text": "Kabel WAN", "severity": "problem", "data": {}},
            {"ts": t0 + 3, "type": "lan_dhcp_restart", "text": "DHCP", "severity": "warning", "data": {}},
            {"ts": t0 + 4, "type": "ping_down", "text": "ping", "severity": "problem",
             "data": {"name": "Google DNS", "kind": "internet"}},
            {"ts": t0 + 15, "type": "backhaul_radio_start", "text": "radio", "severity": "problem",
             "data": {"node": "bedroom"}},
            {"ts": t0 + 31, "type": "backhaul_radio_end", "text": "kabel", "severity": "info",
             "data": {"node": "bedroom"}},
        ]})
        self.assertEqual(inc["cause"], "Zanik kabla WAN")
        self.assertEqual(inc["duration_s"], 31)
        self.assertTrue(inc["summary"].startswith("17:09:34 Zanik kabla WAN, 31 s"))
        self.assertIn("na radiu: bedroom", inc["summary"])
        self.assertIn("brak pingu: Google DNS", inc["summary"])


class MiscTest(unittest.TestCase):
    def test_slugify_like_home_assistant(self):
        self.assertEqual(slugify("Ping Deco główne opóźnienie"), "ping_deco_glowne_opoznienie")
        self.assertEqual(slugify("Switch port 1 błędy RX"), "switch_port_1_bledy_rx")
        self.assertEqual(slugify("living_room backhaul radiowy"), "living_room_backhaul_radiowy")

    def test_switch_page(self):
        html = """<script>var all_info = {
state:[1,1,0,0,0,0,0,0,0,0],
link_status:[6,0,0,0,0,6,5,5,0,0],
pkts:[10,0,20,3,0,0,0,0]
};</script>"""
        info = _parse_all_info(html)
        self.assertEqual(info["link_status"][:3], [6, 0, 0])
        self.assertEqual(info["pkts"][:4], [10, 0, 20, 3])


if __name__ == "__main__":
    unittest.main()
