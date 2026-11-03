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
from syslog_parser import LineDeduper, LogAnalyzer, mac_tail, parse_line  # noqa: E402

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


def L(sec, msg="x"):
    return f"Tue Nov  3 {19 + sec // 3600:02d}:{sec // 60 % 60:02d}:{sec % 60:02d} 2026 daemon.notice {msg}"


class DedupTest(unittest.TestCase):
    def test_growing_buffer(self):
        d = LineDeduper()
        a = [L(i, f"m{i}") for i in range(10)]
        self.assertEqual(d.new_lines(a), a)
        self.assertEqual(d.new_lines(a + [L(10, "m10"), L(11, "m11")]), [L(10, "m10"), L(11, "m11")])
        self.assertEqual(d.new_lines(a), [])

    def test_stale_and_truncated_dumps_add_nothing(self):
        # prawdziwy przypadek: co kilka pobrań router oddaje sam początek bufora
        d = LineDeduper()
        full = [L(i, f"m{i}") for i in range(100)]
        d.new_lines(full)
        self.assertEqual(d.new_lines(full[:38]), [])
        self.assertEqual(d.stale_dumps, 1)
        self.assertEqual(d.new_lines(full + [L(100, "nowa")]), [L(100, "nowa")])

    def test_identical_lines_same_second(self):
        d = LineDeduper()
        same = L(5, "wpa_driver set_key")
        d.new_lines([L(4, "a"), same])
        # drugi identyczny wpis z tej samej sekundy dopisany do bufora później - jest nowy
        self.assertEqual(d.new_lines([L(4, "a"), same, same]), [same])
        self.assertEqual(d.new_lines([L(4, "a"), same, same]), [])

    def test_doubled_dump_old_copies_rejected(self):
        d = LineDeduper()
        a = [L(i, f"m{i}") for i in range(900)]  # bufor Deco ma ~900-4000 linii
        d.new_lines(a)
        self.assertEqual(d.new_lines(a + a + [L(901, "n")]), [L(901, "n")])

    def test_buffer_cleared_by_router(self):
        d = LineDeduper()
        d.new_lines([L(i, f"m{i}") for i in range(50)])
        fresh = [L(200 + i, f"po czyszczeniu {i}") for i in range(3)]
        self.assertEqual(d.new_lines(fresh), fresh)

    def test_restart_remembers_archive(self):
        archive = [L(i, f"m{i}") for i in range(30)]
        d = LineDeduper()
        d.remember(archive)
        self.assertEqual(d.new_lines(archive + [L(31, "nowa")]), [L(31, "nowa")])


class WifiTest(unittest.TestCase):
    def setUp(self):
        self.a = LogAnalyzer()
        self.a.update_nodes(NODES)
        self.a.update_clients({"7C-00-E5-5C-A2-77": "OnePlus-13", "D0-00-B3-44-64-22": "TV TCL"})

    def run_lines(self, lines):
        out = []
        for ln in lines:
            out += self.a.analyze(parse_line(ln))
        return out

    def sta(self, sec, action, mac, iface="ath1"):
        return L(sec, f"hostapd: {iface}: AP-STA-{action} {mac}")

    def test_one_device_repeated_is_not_mass(self):
        ev = self.run_lines([self.sta(i * 5, "DISCONNECTED", "7c:00:e5:5c:a2:77") for i in range(8)])
        self.assertNotIn("wifi_mass_disconnect", [e.type for e in ev])
        flap = [e for e in ev if e.type == "wifi_client_flapping"]
        self.assertEqual(len(flap), 1)
        self.assertIn("OnePlus-13", flap[0].message)

    def test_mass_lists_devices_and_reconnect(self):
        macs = ["7c:00:e5:5c:a2:77", "d0:00:b3:44:64:22", "02:00:00:00:00:03", "02:00:00:00:00:04",
                "02:00:00:00:00:05"]
        ev = self.run_lines([self.sta(i, "DISCONNECTED", m, "ath0" if i % 2 else "ath1")
                             for i, m in enumerate(macs)] + [self.sta(20, "CONNECTED", macs[0])])
        mass = [e for e in ev if e.type == "wifi_mass_disconnect"][0]
        self.assertIn("5 urządzeń", mass.message)
        self.assertIn("OnePlus-13", mass.message)
        self.assertIn("TV TCL", mass.message)
        names = {d["name"]: d for d in mass.data["devices"]}
        self.assertEqual(names["TV TCL"]["band"], "2,4 GHz")
        back = [e for e in ev if e.type == "_wifi_connect"][0]
        self.assertEqual(back.data["back_after_s"], 20)


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

    def test_devices_in_incident(self):
        t0 = datetime(2026, 11, 3, 19, 4, 26).timestamp()
        dev = {"mac": "7c:00:e5:5c:a2:77", "name": "OnePlus-13", "band": "5 GHz"}
        inc = summarize({"items": [
            {"ts": t0, "type": "wifi_mass_disconnect", "text": "masowe", "severity": "problem",
             "data": {"devices": [{**dev, "time": "19:04:26"}]}},
            {"ts": t0 + 2, "type": "_wifi_disconnect", "text": "", "severity": "info", "data": dev},
            {"ts": t0 + 14, "type": "_wifi_connect", "text": "", "severity": "info",
             "data": {**dev, "back_after_s": 12}},
        ]})
        self.assertEqual(inc["devices"][0]["name"], "OnePlus-13")
        self.assertEqual(inc["devices"][0]["wrocil_po_s"], 12)
        self.assertEqual([t["type"] for t in inc["timeline"]], ["wifi_mass_disconnect"])  # bez szczegółów WiFi
        self.assertIn("rozłączone WiFi (1): OnePlus-13", inc["summary"])


class MigrationTest(unittest.TestCase):
    def test_removes_only_unconfirmed_mass_alarms(self):
        import json
        import tempfile

        from migrate import remove_false_alarms
        from store import Store

        st = Store(tempfile.mkdtemp(), 30)
        st.append_log([(None, "Tue Nov  3 19:04:26 2026 daemon.notice hostapd: ath1: AP-STA-DISCONNECTED "
                              "7c:00:e5:5c:a2:77")])
        os.replace(st.log_path(datetime.now().strftime("%Y-%m-%d")), st.log_path("2026-11-03"))
        false_ev = {"time": "2026-11-03T19:04:26", "type": "wifi_mass_disconnect", "severity": "problem",
                    "message": "Masowe rozłączenia WiFi: 6 urządzeń w 60 s", "data": {}}
        real_ev = {"time": "2026-11-03T17:09:34", "type": "wan_link_down", "severity": "problem",
                   "message": "Kabel WAN: brak linku", "data": {}}
        for e in (real_ev, false_ev, false_ev):
            st.add_event(e)
        st.add_incident({"start": "2026-11-03T17:09:34", "summary": "Zanik kabla WAN",
                         "timeline": [{"time": "17:09:34", "type": "wan_link_down", "severity": "problem"}]})
        st.add_incident({"start": "2026-11-03T19:04:26", "summary": "Masowe rozłączenie klientów WiFi",
                         "timeline": [{"time": "19:04:26", "type": "wifi_mass_disconnect", "severity": "problem"}]})
        stats = remove_false_alarms(st, 5, set())
        self.assertEqual([e["type"] for e in st.events(10)], ["wan_link_down"])
        self.assertEqual([i["summary"] for i in st.incidents(10)], ["Zanik kabla WAN"])
        self.assertEqual(stats, {"usuniete_zdarzenia": 2, "usuniete_incydenty": 1})
        self.assertTrue(os.path.exists(st.events_path + ".przed-1.2.1"))


class ImportantFilterTest(unittest.TestCase):
    def test_routine_err_hidden_real_events_kept(self):
        from noise import is_important

        routine = [
            "Tue Nov  3 20:02:41 2026 daemon.err udhcpd[25822]: Sending ACK to 192.168.68.57",
            "Tue Nov  3 20:02:41 2026 daemon.err udhcpd[25822]: clear ip 3944a8c0",
            "Tue Nov  3 20:02:43 2026 daemon.err uhttpd[27483]: uci: Entry not found",
            "Tue Nov  3 20:02:59 2026 daemon.err udhcpd[25822]: Found static lease: 4144a8c0",
            "Tue Nov  3 20:03:02 2026 daemon.err client_mgmt: mac 02-00-00-AA-00-01 had uploaded",
            "Tue Nov  3 20:03:02 2026 daemon.err client_mgmt: client(02-00-00-AA-00-02) not found in arp table "
            "disconnect_counter=0",
            "Tue Nov  3 20:04:05 2026 user.warn : [mobile] [GetEventType] qmi_module=1, qmi_msg_id=0x0051",
        ]
        important = [
            "Tue Nov  3 17:09:34 2026 daemon.notice netifd: Network device 'br-wan' link is down",
            "Tue Nov  3 17:09:37 2026 user.notice smartip: [lan] kill process udhcpd",
            "Tue Nov  3 17:09:46 2026 user.err : Error: ssh_connect: Timeout connecting to 192.168.71.249",
            "Tue Nov  3 20:05:00 2026 daemon.err client_mgmt: client(02-00-00-AA-00-02) not found in arp table "
            "disconnect_counter=4",
            "Tue Nov  3 20:06:00 2026 daemon.err nieznany: coś nowego się zepsuło",
        ]
        self.assertEqual([is_important(l) for l in routine], [False] * len(routine))
        self.assertEqual([is_important(l) for l in important], [True] * len(important))


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
