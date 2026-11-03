"""Rozpoznawanie zdarzeń w syslogu Deco.

Reguły są oparte na prawdziwych wpisach z Deco X50-4G (fw 1.2.0) i X50
(fw 1.8.0) zebranych podczas zaników WAN 2026-11-03.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

LINE_RE = re.compile(
    r"^(?P<ts>[A-Z][a-z]{2} [A-Z][a-z]{2} +\d{1,2} \d\d:\d\d:\d\d \d{4}) "
    r"(?P<fac>[a-z]+)\.(?P<lvl>[a-z]+) (?P<msg>.*)$"
)


@dataclass
class LogLine:
    raw: str
    time: datetime | None
    facility: str
    level: str
    msg: str


def parse_line(raw: str) -> LogLine:
    m = LINE_RE.match(raw)
    if not m:
        return LogLine(raw, None, "", "", raw)
    try:
        ts = datetime.strptime(" ".join(m.group("ts").split()), "%a %b %d %H:%M:%S %Y")
    except ValueError:
        ts = None
    return LogLine(raw, ts, m.group("fac"), m.group("lvl"), m.group("msg"))


@dataclass
class Event:
    type: str
    message: str
    time: datetime
    severity: str = "info"  # info | warning | problem
    data: dict = field(default_factory=dict)
    # zmiany stanów encji wynikające z tego zdarzenia: {klucz: wartość}
    states: dict = field(default_factory=dict)
    # liczniki do zwiększenia
    counters: tuple = ()


# Typy zdarzeń (lista potrzebna dla encji "event" w HA)
EVENT_TYPES = [
    "wan_link_down",
    "wan_link_up",
    "internet_offline",
    "internet_online",
    "wan_lease",
    "clients_kicked",
    "eth_ports_reset",
    "lan_dhcp_restart",
    "failover_to_mobile",
    "failover_to_wan",
    "mesh_node_unreachable",
    "backhaul_radio_start",
    "backhaul_radio_end",
    "wifi_mass_disconnect",
    "router_boot",
    # spoza logu
    "ping_down",
    "ping_up",
    "switch_port_down",
    "switch_port_up",
    "switch_counters_reset",
    "node_offline",
    "node_online",
    "node_backhaul_change",
    "incident",
]

_R = {
    "wan_down": re.compile(r"netifd: Network device 'br-wan' link is down"),
    "wan_up": re.compile(r"netifd: Network device 'br-wan' link is up"),
    "conn": re.compile(r"conn-indicator: Connectivity changed: \w+\(0x\w+\) -> (\w+)\("),
    "lease": re.compile(r"netifd: wan \(\d+\): Lease of ([\d.]+) obtained"),
    "kick": re.compile(r"smartip: \[lan\] disconnect wifi\(lan\) wifi client"),
    "ethrst": re.compile(r"smartip: \[lan\] #+ disconnect eth port (\d+)"),
    "dhcp": re.compile(r"smartip: \[lan\] kill process udhcpd"),
    "mb": re.compile(r"mobile_backup: (ewan_disconnect|ewan_recovered)\s*$"),
    "ssh": re.compile(r"ssh_connect: Timeout connecting to ([\d.]+)"),
    "sta": re.compile(r"hostapd: (ath\d+): AP-STA-(CONNECTED|DISCONNECTED) ([0-9a-fA-F:]{17})"),
    "boot": re.compile(r"(syslogd started|Booting Linux|klogd started: BusyBox)"),
}


def mac_tail(mac: str) -> str:
    """Ostatnie 5 bajtów MAC - satelity łączą się radiowo adresem, który różni
    się od BSSID/MAC tylko pierwszym bajtem (adres lokalnie administrowany)."""
    parts = re.split(r"[:-]", mac.strip().lower())
    return ":".join(parts[-5:]) if len(parts) == 6 else ""


class LogAnalyzer:
    """Zamienia linie logu na zdarzenia. Trzyma mały stan (deduplikacja)."""

    def __init__(self):
        # mac_tail -> {"name":..., "wired": bool}
        self.satellites: dict = {}
        # ip -> nazwa
        self.node_ips: dict = {}
        self._ssh_last: dict = {}
        self._ethrst_last: datetime | None = None
        self._disc_times: list = []
        self._mass_last: datetime | None = None
        self.radio_backhaul: dict = {}  # nazwa -> bool (aktywny backhaul radiowy z logu)
        self._sta_last: dict = {}  # (nazwa, akcja) -> czas
        self._failover_at: datetime | None = None

    def update_nodes(self, nodes: list) -> None:
        """nodes: [{"name", "ip", "macs": [...], "wired": bool, "master": bool}]"""
        sats, ips = {}, {}
        for n in nodes:
            if n.get("ip"):
                ips[n["ip"]] = n["name"]
            if n.get("master"):
                continue
            for mac in n.get("macs", []):
                t = mac_tail(mac)
                if t:
                    sats[t] = {"name": n["name"], "wired": n.get("wired", False)}
        self.satellites, self.node_ips = sats, ips

    def analyze(self, ln: LogLine) -> list:
        if ln.time is None:
            return []
        msg, t = ln.msg, ln.time
        out = []

        if _R["wan_down"].search(msg):
            out.append(Event("wan_link_down", "Kabel WAN: brak linku (fizyczne rozłączenie)", t,
                             "problem", states={"wan_link": False}, counters=("wan_link_drops",)))
        elif _R["wan_up"].search(msg):
            out.append(Event("wan_link_up", "Kabel WAN: link przywrócony", t, states={"wan_link": True}))
        elif m := _R["conn"].search(msg):
            online = m.group(1).upper() == "ONLINE"
            if online:
                out.append(Event("internet_online", "Internet: Deco znów online", t, states={"internet": True}))
            else:
                out.append(Event("internet_offline", "Internet: Deco straciło dostęp do internetu", t,
                                 "problem", states={"internet": False}, counters=("internet_drops",)))
        elif m := _R["lease"].search(msg):
            out.append(Event("wan_lease", f"WAN: adres {m.group(1)} od operatora", t,
                             data={"ip": m.group(1)}, states={"wan_ip": m.group(1)}))
        elif _R["kick"].search(msg):
            out.append(Event("clients_kicked", "Deco rozłączyło WSZYSTKICH klientów WiFi (zmiana DNS)", t,
                             "problem", counters=("clients_kicked",)))
        elif m := _R["ethrst"].search(msg):
            if self._ethrst_last is None or (t - self._ethrst_last).total_seconds() > 10:
                out.append(Event("eth_ports_reset", "Deco zresetowało porty Ethernet sieci lokalnej", t,
                                 "problem", data={"port": int(m.group(1))}, counters=("eth_ports_reset",)))
            self._ethrst_last = t
        elif _R["dhcp"].search(msg):
            out.append(Event("lan_dhcp_restart", "Restart serwera DHCP sieci lokalnej", t, "warning",
                             counters=("lan_dhcp_restarts",)))
        elif m := _R["mb"].search(msg):
            if m.group(1) == "ewan_disconnect":
                self._failover_at = t
                out.append(Event("failover_to_mobile", "Łącze zapasowe: przełączenie WAN → LTE", t, "problem",
                                 counters=("failovers",)))
            elif self._failover_at and (t - self._failover_at).total_seconds() < 1800:
                # "ewan_recovered" pojawia się też przy wyłączonym łączu zapasowym,
                # więc raportujemy powrót tylko po faktycznym przełączeniu na LTE
                self._failover_at = None
                out.append(Event("failover_to_wan", "Łącze zapasowe: powrót LTE → WAN", t, "warning"))
        elif m := _R["ssh"].search(msg):
            ip = m.group(1)
            last = self._ssh_last.get(ip)
            if last is None or (t - last).total_seconds() > 120:
                name = self.node_ips.get(ip, ip)
                out.append(Event("mesh_node_unreachable", f"Główne Deco nie może połączyć się z satelitą {name}",
                                 t, "warning", data={"node": name, "ip": ip}, counters=("mesh_unreachable",)))
            self._ssh_last[ip] = t
        elif m := _R["sta"].search(msg):
            iface, action, mac = m.group(1), m.group(2), m.group(3)
            sat = self.satellites.get(mac_tail(mac))
            if sat:
                name = sat["name"]
                key = (name, action)
                last = self._sta_last.get(key)
                self._sta_last[key] = t
                if last is not None and (t - last).total_seconds() <= 5:
                    return out  # to samo zdarzenie na drugim paśmie
                if action == "CONNECTED":
                    self.radio_backhaul[name] = True
                    if sat["wired"]:
                        text = f"Satelita {name}: zgubiła kabel, łączy się z głównym Deco przez radio"
                        sev = "problem"
                    else:
                        text = f"Satelita {name}: ponownie połączona radiowo z głównym Deco"
                        sev = "warning"
                    out.append(Event("backhaul_radio_start", text, t, sev, data={"node": name, "iface": iface},
                                     states={f"radio_backhaul:{name}": True}, counters=("backhaul_fallbacks",)
                                     if sat["wired"] else ()))
                else:
                    self.radio_backhaul[name] = False
                    text = (f"Satelita {name}: wróciła na kabel" if sat["wired"]
                            else f"Satelita {name}: rozłączyła łącze radiowe z głównym Deco")
                    out.append(Event("backhaul_radio_end", text, t, "info" if sat["wired"] else "warning",
                                     data={"node": name, "iface": iface},
                                     states={f"radio_backhaul:{name}": False}))
            else:
                if action == "CONNECTED":
                    out.append(Event("_wifi_connect", "", t, counters=("wifi_connects",)))
                else:
                    out.append(Event("_wifi_disconnect", "", t, counters=("wifi_disconnects",)))
                    self._disc_times = [x for x in self._disc_times if (t - x).total_seconds() <= 60] + [t]
                    if len(self._disc_times) >= 6 and (
                        self._mass_last is None or (t - self._mass_last).total_seconds() > 120
                    ):
                        self._mass_last = t
                        out.append(Event("wifi_mass_disconnect",
                                         f"Masowe rozłączenia WiFi: {len(self._disc_times)} urządzeń w 60 s",
                                         t, "problem", counters=("wifi_mass_disconnects",)))
        elif _R["boot"].search(msg):
            out.append(Event("router_boot", "Główne Deco uruchomiło się ponownie", t, "problem",
                             counters=("router_boots",)))
        return out


class LineDiffer:
    """Wyznacza nowe linie między kolejnymi zrzutami bufora.

    Bufor routera tylko przesuwa się do przodu (stare linie wypadają z początku),
    więc koniec poprzedniego zrzutu leży w nowym zrzucie na tej samej pozycji albo
    wcześniej. Szukamy go od poprzedniej pozycji w dół - to ważne, bo w logu Deco
    często powtarzają się identyczne linie.
    """

    ANCHOR = 5

    def __init__(self):
        self.prev_tail: list = []
        self.prev_len: int | None = None

    def new_lines(self, lines: list) -> list:
        prev_tail, prev_len = self.prev_tail, self.prev_len
        if lines:
            self.prev_tail, self.prev_len = lines[-self.ANCHOR :], len(lines)
        if not prev_tail:
            return lines  # pierwszy zrzut: cały bufor (zapis do archiwum)
        start = self._find(lines, prev_tail, prev_len)
        if start is None:
            start = self._find(lines, prev_tail[-1:], prev_len)
        return lines if start is None else lines[start:]

    @staticmethod
    def _find(lines: list, anchor: list, prev_len: int | None):
        k = len(anchor)
        top = len(lines) - k
        if prev_len is not None:
            top = min(top, prev_len - k)
        for i in range(top, -1, -1):
            if lines[i : i + k] == anchor:
                return i + k
        return None
