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

    def test_internet_needs_all_targets_down(self):
        t0 = datetime(2026, 11, 9, 9, 12, 24).timestamp()

        def ping(ts, typ, name, kind):
            return {"ts": ts, "type": typ, "text": "ping", "severity": "problem" if typ == "ping_down" else "info",
                    "data": {"name": name, "kind": kind}}
        hallway = [ping(t0, "ping_down", "Satelita hallway", "mesh"), ping(t0 + 6000, "ping_up", "Satelita hallway", "mesh")]
        cf = [ping(t0 + 100, "ping_down", "Cloudflare", "internet"), ping(t0 + 105, "ping_up", "Cloudflare", "internet")]
        targets = {"Google DNS", "Cloudflare"}
        # 9.11: 5 s bez Cloudflare w środku 2-godzinnej pętli hallway - to nie był brak internetu
        inc = summarize({"items": hallway + cf}, targets)
        self.assertEqual(inc["cause"], "Satelita niedostępna: Satelita hallway")
        self.assertIn("brak pingu: Cloudflare, Satelita hallway", inc["summary"])
        inc = summarize({"items": cf}, targets)
        self.assertEqual(inc["cause"], "Brak pingu do Cloudflare (internet działał)")
        # oba cele naraz -> brak internetu
        both = cf[:1] + [ping(t0 + 101, "ping_down", "Google DNS", "internet")] + cf[1:]
        self.assertEqual(summarize({"items": hallway + both}, targets)["cause"], "Brak internetu (kabel WAN OK)")
        # oba, ale nie jednocześnie -> nie
        later = [ping(t0 + 200, "ping_down", "Google DNS", "internet"), ping(t0 + 203, "ping_up", "Google DNS", "internet")]
        self.assertNotEqual(summarize({"items": cf + later}, targets)["cause"], "Brak internetu (kabel WAN OK)")
        # zgłoszenie od Deco wystarcza; bez listy celów - zachowanie jak dawniej
        offline = [{"ts": t0 + 50, "type": "internet_offline", "text": "Deco", "severity": "problem", "data": {}}]
        self.assertEqual(summarize({"items": hallway + offline}, targets)["cause"], "Brak internetu (kabel WAN OK)")
        self.assertEqual(summarize({"items": hallway + cf})["cause"], "Brak internetu (kabel WAN OK)")

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


class OfflineNodeNameTest(unittest.TestCase):
    def test_unreachable_offline_node_keeps_name(self):
        a = LogAnalyzer()
        a.update_nodes([{"name": "hallway", "ip": "192.168.71.248", "macs": []}])
        a.update_nodes([{"name": "hallway", "ip": None, "macs": []}])  # offline: Deco nie podaje IP
        ev = a.analyze(parse_line("Mon Nov  9 10:44:18 2026 user.err : Error: ssh_connect: "
                                  "Timeout connecting to 192.168.71.248"))
        un = [e for e in ev if e.type == "mesh_node_unreachable"]
        self.assertEqual(un[0].data["node"], "hallway")
        self.assertIn("satelitą hallway", un[0].message)

    def test_timeline_merges_ip_lanes(self):
        import time as _t
        from types import SimpleNamespace

        import viz
        now = _t.time()
        iso = datetime.fromtimestamp(now - 60).isoformat(timespec="seconds")
        evs = [{"time": iso, "type": "mesh_node_unreachable", "severity": "warning",
                "data": {"node": "192.168.71.248", "ip": "192.168.71.248"}}]
        store = SimpleNamespace(events_since=lambda ts: evs, incidents_since=lambda ts: [])
        mon = SimpleNamespace(ping_meta={}, pinger=SimpleNamespace(targets={}), values={},
                              nodes={"hallway": {"master": False, "ip": None, "wired": False}},
                              analyzer=SimpleNamespace(node_ips={"192.168.71.248": "hallway"}))
        names = [ln["name"] for ln in viz.timeline(store, mon, 1)["lanes"]]
        self.assertIn("Satelita hallway", names)
        self.assertNotIn("Satelita 192.168.71.248", names)


class RemovedNodeTest(unittest.TestCase):
    """1.3.5: węzeł usunięty z sieci Deco (np. hallway M3W) znika z osi czasu, pingu i HA."""

    def _timeline(self, evs, nodes, ping_meta=None):
        from types import SimpleNamespace

        import viz
        store = SimpleNamespace(events_since=lambda ts: evs, incidents_since=lambda ts: [])
        mon = SimpleNamespace(ping_meta=ping_meta or {}, pinger=SimpleNamespace(targets={}), values={},
                              nodes=nodes, analyzer=SimpleNamespace(node_ips={"192.168.71.248": "hallway"}))
        return {ln["name"]: ln for ln in viz.timeline(store, mon, 1)["lanes"]}

    @staticmethod
    def _ev(ago, typ, data):
        import time as _t
        iso = datetime.fromtimestamp(_t.time() - ago).isoformat(timespec="seconds")
        return {"time": iso, "type": typ, "severity": "problem", "data": data}

    NODES = {"Poddasze": {"master": True, "ip": "192.168.68.1"},
             "bedroom": {"master": False, "ip": "192.168.71.249", "wired": False}}

    def test_removed_satellite_lanes_hidden(self):
        ping = {"name": "Satelita hallway", "host": "192.168.71.248", "kind": "mesh"}
        evs = [self._ev(900, "node_offline", {"node": "hallway"}),
               self._ev(800, "ping_down", ping), self._ev(700, "ping_up", ping),
               self._ev(600, "ping_down", ping),  # nigdy nie zamknięty: węzeł usunięto
               self._ev(500, "mesh_node_unreachable", {"node": "192.168.71.248"}),
               self._ev(400, "node_offline", {"node": "bedroom"})]
        lanes = self._timeline(evs, self.NODES)
        self.assertNotIn("Satelita hallway", lanes)
        self.assertNotIn("Ping Satelita hallway", lanes)
        self.assertIn("Satelita bedroom", lanes)

    def test_nothing_hidden_before_first_device_list(self):
        evs = [self._ev(400, "node_offline", {"node": "bedroom"}), self._ev(300, "node_online", {"node": "bedroom"})]
        self.assertIn("Satelita bedroom", self._timeline(evs, {}))

    def test_unclosed_ping_keeps_kind_from_event(self):
        ping = {"name": "Stary cel", "host": "10.0.0.9", "kind": "internet"}
        lanes = self._timeline([self._ev(300, "ping_down", ping)], self.NODES)
        self.assertEqual(lanes["Ping Stary cel"]["group"], "internet")

    def test_forget_node_after_missing_polls(self):
        from types import SimpleNamespace
        from unittest import mock

        import main
        fake = SimpleNamespace(nodes={"Poddasze": {}, "bedroom": {}, "hallway": {}}, _node_missing={},
                               ping_meta={"mesh_hallway": {}, "mesh_bedroom": {}},
                               ents={"node_hallway_online": "binary_sensor.deco_monitor_hallway_online",
                                     "node_hallway_conn": "sensor.deco_monitor_hallway_polaczenie",
                                     "ping_mesh_hallway": "binary_sensor.deco_monitor_ping_satelita_hallway",
                                     "ping_mesh_hallway_rtt": "x_rtt", "ping_mesh_hallway_loss": "x_loss",
                                     "node_bedroom_online": "binary_sensor.deco_monitor_bedroom_online"},
                               values={}, pub=mock.Mock(), pinger=mock.Mock(), satlogs=mock.Mock())
        fake._forget_node = lambda name: main.Monitor._forget_node(fake, name)
        for _ in range(main.NODE_GONE_POLLS - 1):
            main.Monitor._prune_missing_nodes(fake, {"Poddasze", "bedroom"})
        self.assertIn("hallway", fake.nodes)
        main.Monitor._prune_missing_nodes(fake, {"Poddasze", "bedroom"})
        self.assertNotIn("hallway", fake.nodes)
        self.assertEqual(sorted(fake.ents), ["node_bedroom_online"])
        self.assertEqual(fake.pub.remove.call_count, 5)
        fake.pinger.remove_target.assert_called_once_with("mesh_hallway")
        fake.satlogs.remove_node.assert_called_once_with("hallway")
        self.assertNotIn("mesh_hallway", fake.ping_meta)

    def test_node_back_resets_counter(self):
        from types import SimpleNamespace
        from unittest import mock

        import main
        fake = SimpleNamespace(nodes={"hallway": {}}, _node_missing={}, _forget_node=mock.Mock())
        for _ in range(main.NODE_GONE_POLLS - 1):
            main.Monitor._prune_missing_nodes(fake, set())
        main.Monitor._prune_missing_nodes(fake, {"hallway"})
        for _ in range(main.NODE_GONE_POLLS - 1):
            main.Monitor._prune_missing_nodes(fake, set())
        fake._forget_node.assert_not_called()

    def test_publisher_remove_and_stale_topics(self):
        from mqtt_pub import Publisher, is_stale_discovery_topic
        p = Publisher(None, 1883, None, None, "t")
        sent = []
        p._raw = lambda topic, payload, retain=False: sent.append((topic, payload, retain))
        key = p.entity("sensor", "hallway połączenie", attrs=True)
        p.state(key, "radio")
        sent.clear()
        p.remove(key)
        self.assertIn(("homeassistant/sensor/deco_monitor/deco_monitor_hallway_polaczenie/config", "", True), sent)
        self.assertIn(("deco_monitor/sensor/hallway_polaczenie/state", "", True), sent)
        self.assertEqual(p.entity_ids(), [])
        sent.clear()
        p._republish()
        self.assertNotIn("deco_monitor/sensor/hallway_polaczenie/state", [t for t, _, _ in sent])
        self.assertTrue(is_stale_discovery_topic(
            "homeassistant/sensor/deco_monitor/sensor.deco_monitor_hallway_polaczenie/config"))
        self.assertFalse(is_stale_discovery_topic(
            "homeassistant/sensor/deco_monitor/deco_monitor_hallway_polaczenie/config"))

    def test_republish_uses_discovery_topic(self):
        from mqtt_pub import Publisher
        p = Publisher(None, 1883, None, None, "t")
        sent = []
        p._raw = lambda topic, payload, retain=False: sent.append(topic)
        p.entity("binary_sensor", "bedroom online")
        sent.clear()
        p._republish()
        self.assertIn("homeassistant/binary_sensor/deco_monitor/deco_monitor_bedroom_online/config", sent)
        self.assertFalse([t for t in sent if "/binary_sensor.deco_monitor" in t])


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
