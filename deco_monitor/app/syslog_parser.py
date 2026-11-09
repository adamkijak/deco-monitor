"""Rozpoznawanie zdarzeń w syslogu Deco.

Reguły są oparte na prawdziwych wpisach z Deco X50-4G (fw 1.2.0) i X50
(fw 1.8.0) zebranych podczas zaników WAN 2026-11-03.
"""

from __future__ import annotations

import re
from collections import Counter, deque
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
    "wifi_client_flapping",
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
    "node_backhaul_speed",
    "node_parent_change",
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


BANDS = {"ath0": "2,4 GHz", "ath1": "5 GHz", "ath2": "5 GHz-2", "ath3": "6 GHz"}


class LogAnalyzer:
    """Zamienia linie logu na zdarzenia. Trzyma mały stan (deduplikacja)."""

    MASS_DEVICES = 5  # tyle różnych urządzeń rozłączonych w 60 s = masowe rozłączenie

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
        self.client_names: dict = {}  # mac (aa:bb:..) -> nazwa z aplikacji Deco
        self._last_disc: dict = {}  # mac -> czas rozłączenia (do "wrócił po N s")
        self._disc_win: list = []  # (czas, mac, info) z ostatnich 60 s
        self._flap: dict = {}  # mac -> [czasy rozłączeń z 10 min]
        self._flap_last: dict = {}

    def update_clients(self, clients: dict) -> None:
        """clients: {mac w dowolnym formacie: nazwa}"""
        self.client_names = {m.lower().replace("-", ":"): n for m, n in clients.items() if m}

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
        # IP -> nazwa zapamiętujemy na stałe: węzeł offline nie ma device_ip w liście węzłów, a właśnie
        # wtedy w logu pojawia się "ssh_connect: Timeout connecting to <IP>"
        self.satellites, self.node_ips = sats, {**self.node_ips, **ips}

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
                out += self._client_event(iface, action, mac.lower(), t)
        elif _R["boot"].search(msg):
            out.append(Event("router_boot", "Główne Deco uruchomiło się ponownie", t, "problem",
                             counters=("router_boots",)))
        return out


    def _client_event(self, iface: str, action: str, mac: str, t: datetime) -> list:
        name = self.client_names.get(mac) or mac
        band = BANDS.get(iface, iface)
        info = {"mac": mac, "name": name, "band": band, "iface": iface}
        if action == "CONNECTED":
            last = self._last_disc.pop(mac, None)
            back = round((t - last).total_seconds()) if last and (t - last).total_seconds() <= 900 else None
            if back is not None:
                info["back_after_s"] = back
            text = f"WiFi: {name} połączony ({band})" + (f", wrócił po {back} s" if back is not None else "")
            return [Event("_wifi_connect", text, t, data=info, counters=("wifi_connects",))]

        out = [Event("_wifi_disconnect", f"WiFi: {name} rozłączony ({band})", t, data=info,
                     counters=("wifi_disconnects",))]
        self._last_disc[mac] = t
        # masowe: RÓŻNE urządzenia w 60 s (nie kolejne linie tego samego urządzenia)
        self._disc_win = [w for w in self._disc_win if (t - w[0]).total_seconds() <= 60] + [(t, mac, info)]
        devices: dict = {}
        for x, m, d in self._disc_win:
            devices.setdefault(m, {**d, "time": x.strftime("%H:%M:%S"), "ts": x.timestamp()})
        if len(devices) >= self.MASS_DEVICES and (
                self._mass_last is None or (t - self._mass_last).total_seconds() > 120):
            self._mass_last = t
            names = [d["name"] for d in devices.values()]
            more = f" (+{len(names) - 6})" if len(names) > 6 else ""
            out.append(Event("wifi_mass_disconnect",
                             f"Masowe rozłączenia WiFi na głównym Deco: {len(devices)} urządzeń w 60 s – "
                             + ", ".join(names[:6]) + more,
                             t, "problem", data={"devices": list(devices.values())},
                             counters=("wifi_mass_disconnects",)))
        # pojedyncze urządzenie, które co chwilę się rozłącza
        hist = [x for x in self._flap.get(mac, []) if (t - x).total_seconds() <= 600] + [t]
        self._flap[mac] = hist
        last_flap = self._flap_last.get(mac)
        if len(hist) >= 4 and (last_flap is None or (t - last_flap).total_seconds() > 1800):
            self._flap_last[mac] = t
            out.append(Event("wifi_client_flapping", f"WiFi: {name} rozłączył się {len(hist)}× w 10 min ({band})",
                             t, "warning", data={**info, "count": len(hist)}, counters=("wifi_flapping",)))
        if len(self._flap) > 1000:
            self._flap = {m: h for m, h in self._flap.items() if (t - h[-1]).total_seconds() <= 600}
        return out


class LineDeduper:
    """Wybiera z kolejnego zrzutu bufora linie, których jeszcze nie ma w archiwum.

    Eksport logu z Deco bywa nieaktualny albo ucięty (co kilka pobrań router oddaje
    sam początek bufora), a bufor co jakiś czas się czyści. Dlatego nie porównujemy
    zrzutów ze sobą, tylko każdą linię z tym, co już zapisaliśmy - z krotnością, bo w logu
    są identyczne linie z tej samej sekundy. Linia znana i wyraźnie starsza niż najnowszy
    zapisany wpis to kopia, nawet jeśli w zrzucie występuje częściej.
    """

    HORIZON = 3 * 3600  # jak długo pamiętamy zapisane linie
    SLACK = 5  # s: wpisy różnych procesów bywają lekko nie po kolei

    def __init__(self):
        self.seen: Counter = Counter()
        self._order: deque = deque()  # (czas, hash)
        self.newest: float | None = None
        self.stale_dumps = 0

    def _add(self, h: int, ts: float | None) -> None:
        self.seen[h] += 1
        self._order.append((ts if ts is not None else (self.newest or 0), h))
        if ts is not None and (self.newest is None or ts > self.newest):
            self.newest = ts

    def _expire(self) -> None:
        if self.newest is None:
            return
        while self._order and self._order[0][0] < self.newest - self.HORIZON:
            _, h = self._order.popleft()
            self.seen[h] -= 1
            if self.seen[h] <= 0:
                del self.seen[h]

    def remember(self, lines: list) -> None:
        """Odbudowa pamięci z archiwum po starcie dodatku."""
        last = None
        for line in lines:
            ts = line_epoch(line)
            last = ts if ts is not None else last
            self._add(hash(line), last)
        self._expire()

    NEAR = 200  # identyczne wpisy z tej samej sekundy leżą w buforze tuż obok siebie

    def new_lines(self, lines: list) -> list:
        dump: Counter = Counter()
        pos: dict = {}
        out, last = [], None
        for idx, line in enumerate(lines):
            ts = line_epoch(line)
            last = ts if ts is not None else last
            h = hash(line)
            dump[h] += 1
            prev_pos, pos[h] = pos.get(h), idx
            known = self.seen.get(h, 0)
            if dump[h] <= known:
                continue
            if known:
                if last is not None and self.newest is not None and last < self.newest - self.SLACK:
                    continue  # kolejna kopia starej linii
                if prev_pos is not None and idx - prev_pos > self.NEAR:
                    continue  # ta sama linia daleko w zrzucie = zawartość podwojona
            out.append(line)
            self._add(h, last)
        if lines and not out:
            end = line_epoch(lines[-1])
            if end is not None and self.newest is not None and end < self.newest - self.SLACK:
                self.stale_dumps += 1
        self._expire()
        return out


_EPOCH_CACHE: dict = {}


def line_epoch(line: str) -> float | None:
    """Znacznik czasu linii logu Deco jako epoch (bez pełnego parsowania)."""
    head = line[:24]
    if len(head) < 24 or head[3] != " " or head[19] != " ":
        return None
    ts = _EPOCH_CACHE.get(head)
    if ts is None:
        try:
            ts = datetime.strptime(" ".join(head.split()), "%a %b %d %H:%M:%S %Y").timestamp()
        except ValueError:
            return None
        if len(_EPOCH_CACHE) > 50000:
            _EPOCH_CACHE.clear()
        _EPOCH_CACHE[head] = ts
    return ts
