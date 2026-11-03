#!/usr/bin/env python3
"""Deco Monitor - dodatek Home Assistant.

Źródła danych:
  * syslog głównego Deco (eksport logu, co `syslog_interval` s) -> archiwum + zdarzenia
  * lokalne API Deco (węzły mesh, backhaul, WAN, CPU/RAM, klienci)
  * switch TP-Link Easy Smart (link/ruch/błędy portów)
  * ping co `ping_interval` s (internet, sieć lokalna, satelity)
Wszystko trafia do HA przez MQTT discovery; incydenty są składane z sygnałów.
"""

from __future__ import annotations

import hashlib
import heapq
import itertools
import json
import logging
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dashboard import build_config, build_dashboard  # noqa: E402
from deco_api import DecoClient, DecoError, DecoTransientError, b64_name  # noqa: E402
from ha_ws import HAError, HomeAssistantWS  # noqa: E402
from incidents import IncidentTracker  # noqa: E402
from migrate import remove_false_alarms  # noqa: E402
from noise import NOISE_RE  # noqa: E402
from mqtt_pub import Publisher, slugify, supervisor_mqtt  # noqa: E402
from pinger import Pinger  # noqa: E402
from store import Store  # noqa: E402
from switch_api import EasySmartSwitch, SwitchError  # noqa: E402
from syslog_parser import EVENT_TYPES, LineDeduper, LogAnalyzer, parse_line  # noqa: E402
from webui import start_webui  # noqa: E402

VERSION = "1.2.3"
log = logging.getLogger("deco_monitor")

COUNTERS = {
    "wan_link_drops": ("Zaniki kabla WAN", "mdi:ethernet-cable-off"),
    "internet_drops": ("Zaniki internetu", "mdi:web-off"),
    "clients_kicked": ("Wyrzucenia klientów przez Deco", "mdi:account-multiple-remove"),
    "eth_ports_reset": ("Resety portów Ethernet", "mdi:ethernet"),
    "lan_dhcp_restarts": ("Restarty DHCP LAN", "mdi:ip-network"),
    "failovers": ("Przełączenia na LTE", "mdi:sim"),
    "mesh_unreachable": ("Niedostępność satelit", "mdi:access-point-network-off"),
    "backhaul_fallbacks": ("Utraty kabla satelit", "mdi:lan-disconnect"),
    "wifi_connects": ("Połączenia WiFi", "mdi:wifi-plus"),
    "wifi_disconnects": ("Rozłączenia WiFi", "mdi:wifi-minus"),
    "wifi_mass_disconnects": ("Masowe rozłączenia WiFi", "mdi:wifi-alert"),
    "wifi_flapping": ("Niestabilne urządzenia WiFi", "mdi:wifi-sync"),
    "router_boots": ("Restarty Deco", "mdi:restart"),
    "incidents": ("Incydenty", "mdi:alert-octagon"),
    "ping_outages": ("Zaniki ping", "mdi:lan-pending"),
    "switch_link_drops": ("Utraty linku na switchu", "mdi:ethernet-off"),
    "switch_resets": ("Restarty switcha", "mdi:restart-alert"),
}

CONN_TEXT = {"wired": "kabel", "band5": "radio 5 GHz", "band2_4": "radio 2,4 GHz", "band6": "radio 6 GHz"}


def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def config_hash(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


DASH_URL = "siec-deco"



def save_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
    os.replace(tmp, path)


class Monitor:
    def __init__(self, opts: dict):
        self.opts = opts
        self.data_dir = os.environ.get("DECO_MONITOR_DATA", "/data")
        self.share_dir = os.environ.get("DECO_MONITOR_SHARE", "/share/deco_monitor")
        os.makedirs(self.data_dir, exist_ok=True)
        self.store = Store(self.share_dir, int(opts.get("log_retention_days", 30)))

        sup = supervisor_mqtt()
        self.pub = Publisher(
            opts.get("mqtt_host") or sup.get("host"),
            int(opts.get("mqtt_port") or sup.get("port") or 1883),
            opts.get("mqtt_username") or sup.get("username"),
            opts.get("mqtt_password") or sup.get("password"),
            VERSION,
            dry_run=os.environ.get("DECO_MONITOR_DRY_RUN") == "1",
        )

        self.deco = DecoClient(opts["deco_host"], opts["deco_password"])
        self.analyzer = LogAnalyzer()
        self.dedup = LineDeduper()
        self.syslog_interval = float(opts.get("syslog_interval", 10))
        # zdarzenia z logu publikujemy z przesunięciem `lag`, zachowując odstępy czasowe
        self.lag = self.syslog_interval + 5
        self.incidents = IncidentTracker(self.lag + 20, self._incident_closed)
        self.pinger = Pinger(float(opts.get("ping_interval", 2)), 2, self._ping_change)

        self.counters = load_json(os.path.join(self.data_dir, "counters.json"), {})
        self.persist = load_json(os.path.join(self.data_dir, "state.json"), {})
        self._persist_lock = threading.Lock()
        if not self.persist.get("archive_deduped_1_2"):
            # wersje < 1.2.0 dopisywały nieaktualne zrzuty bufora drugi raz
            removed = self.store.dedupe_archive()
            if removed:
                log.warning("Usunięto %d zduplikowanych linii z archiwum (kopie: *.przed-1.2.0)", removed)
            self.persist["archive_deduped_1_2"] = True
            self.persist.pop("anchor", None)
            self.persist.pop("anchor_len", None)
            save_json(os.path.join(self.data_dir, "state.json"), self.persist)
        self.dedup.remember(self.store.tail_lines())

        self.ents: dict = {}
        self.values: dict = {}  # ostatnie wartości (dla web UI)
        self.nodes: dict = {}  # nazwa -> info
        self.ping_meta: dict = {}  # key -> {"name","kind","host"}
        self.log_state_at: dict = {}  # klucz stanu -> czas ostatniej zmiany z logu
        self.recent_lines = deque(maxlen=40)
        self.status = {"deco": "start", "switch": None, "syslog_at": None}

        self._sched: list = []
        self._sched_seq = itertools.count()
        self._sched_cv = threading.Condition()
        self._dash_sig = None
        self._first_syslog = True

        self.switch = None
        if opts.get("switch_host") and opts.get("switch_password"):
            self.switch = EasySmartSwitch(opts["switch_host"], opts.get("switch_username") or "admin",
                                          opts["switch_password"])
        names = list(opts.get("switch_port_names") or [])
        self.port_labels = {i + 1: (names[i] if i < len(names) and names[i] else f"port {i + 1}")
                            for i in range(8)}
        self.port_prev: dict = {}
        self.port_up: dict = {}
        self.port_down_since: dict = {}
        self.port_rate_at = 0.0
        self.port_rate_prev: dict = {}
        self.port_rates: dict = {}
        self._switch_fails = 0

        self.ingress_url = self._ingress_url()
        self._register_static()

    @staticmethod
    def _ingress_url() -> str:
        """Adres panelu dodatku w HA (slug zależy od repozytorium, z którego go zainstalowano)."""
        token = os.environ.get("SUPERVISOR_TOKEN")
        if token:
            import urllib.request

            req = urllib.request.Request("http://supervisor/addons/self/info",
                                         headers={"Authorization": f"Bearer {token}"})
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    slug = json.loads(resp.read())["data"]["slug"]
                return f"/hassio/ingress/{slug}"
            except Exception as exc:
                log.warning("Nie udało się odczytać sluga dodatku: %s", exc)
        return "/hassio/ingress/local_deco_monitor"

    # ------------------------------------------------------------------ encje
    def ent(self, key: str, domain: str, name: str, **kw) -> str:
        if key not in self.ents:
            self.ents[key] = self.pub.entity(domain, name, **kw)
        return self.ents[key]

    def set(self, key: str, value, attrs: dict | None = None) -> None:
        self.values[key] = value
        self.pub.state(self.ents[key], value)
        if attrs is not None:
            self.pub.attrs(self.ents[key], attrs)

    def _register_static(self) -> None:
        conn = {"device_class": "connectivity"}
        self.ent("wan_link", "binary_sensor", "Kabel WAN", icon="mdi:ethernet-cable", **conn)
        self.ent("internet", "binary_sensor", "Internet", **conn)
        self.ent("wan_ip", "sensor", "Adres IP WAN", icon="mdi:ip")
        self.ent("cpu", "sensor", "CPU Deco", unit_of_measurement="%", state_class="measurement",
                 icon="mdi:cpu-64-bit")
        self.ent("mem", "sensor", "RAM Deco", unit_of_measurement="%", state_class="measurement",
                 icon="mdi:memory")
        self.ent("clients_wifi", "sensor", "Klienci WiFi", state_class="measurement", icon="mdi:wifi")
        self.ent("clients_wired", "sensor", "Klienci przewodowi", state_class="measurement", icon="mdi:lan")
        self.ent("last_event", "sensor", "Ostatnie zdarzenie", attrs=True, icon="mdi:bell-ring")
        self.ent("last_incident", "sensor", "Ostatni incydent", attrs=True, icon="mdi:alert-octagon")
        self.ent("incident_active", "binary_sensor", "Trwa incydent", device_class="problem")
        self.ent("log_line", "sensor", "Log ostatnie linie", attrs=True, icon="mdi:text-box-outline")
        self.ent("events", "event", "Zdarzenia", event_types=EVENT_TYPES, icon="mdi:timeline-alert")
        self.ent("monitor_status", "sensor", "Status monitora", attrs=True, icon="mdi:heart-pulse",
                 entity_category="diagnostic")
        for key, (name, icon) in COUNTERS.items():
            self.ent(f"cnt_{key}", "sensor", name, icon=icon, state_class="total_increasing")
            self.set(f"cnt_{key}", int(self.counters.get(key, 0)))
        self.set("incident_active", False)
        self._publish_history()
        for t in self.opts.get("ping_targets") or []:
            self.add_ping_target(f"t_{slugify(t['name'])}", t["name"], t["host"], t.get("kind", "local"))
        if self.switch:
            for n in range(1, 9):
                self._register_port(n)

    def _publish_history(self) -> None:
        """Ostatni incydent i ostatnie zdarzenie z archiwum (np. po starcie albo po migracji)."""
        inc = self.store.incidents(1)
        if inc:
            self.set("last_incident", inc[0]["summary"], {**inc[0], "log": inc[0].get("log", [])[:40]})
        else:
            self.set("last_incident", "brak", {})
        ev = [e for e in self.store.events(20) if not e.get("historyczne")]
        if ev:
            e = ev[0]
            self.set("last_event", f"{e['time'][11:19]} {e['message']}"[:255],
                     {"typ": e["type"], "waga": e["severity"], "czas": e["time"], **(e.get("data") or {})})

    def _remove_false_alarms(self) -> None:
        if self.persist.get("false_alarms_removed_1_2_1"):
            return
        try:
            stats = remove_false_alarms(self.store, self.analyzer.MASS_DEVICES, set(self.analyzer.satellites))
            if stats["usuniete_zdarzenia"] or stats["usuniete_incydenty"]:
                log.warning("Usunięto fałszywe alarmy sprzed wersji 1.2.0: %d zdarzeń, %d incydentów",
                            stats["usuniete_zdarzenia"], stats["usuniete_incydenty"])
                self._publish_history()
        except Exception:
            log.exception("Nie udało się usunąć starych fałszywych alarmów")
            return
        self.persist["false_alarms_removed_1_2_1"] = True
        self.save_persist()

    def add_ping_target(self, key: str, name: str, host: str, kind: str) -> None:
        if not self.pinger.set_target(key, name, host, kind):
            return
        self.ping_meta[key] = {"name": name, "kind": kind, "host": host}
        self.ent(f"ping_{key}", "binary_sensor", f"Ping {name}", device_class="connectivity")
        self.ent(f"ping_{key}_rtt", "sensor", f"Ping {name} opóźnienie", unit_of_measurement="ms",
                 state_class="measurement", icon="mdi:timer-outline", suggested_display_precision=1)
        self.ent(f"ping_{key}_loss", "sensor", f"Ping {name} strata", unit_of_measurement="%",
                 state_class="measurement", icon="mdi:package-variant-remove")

    def _register_port(self, n: int) -> None:
        self.ent(f"port{n}", "sensor", f"Switch port {n}", attrs=True, icon="mdi:ethernet")
        self.ent(f"port{n}_link", "binary_sensor", f"Switch port {n} link", device_class="connectivity")
        self.ent(f"port{n}_rxbad", "sensor", f"Switch port {n} błędy RX", state_class="total_increasing",
                 icon="mdi:alert-circle-outline")
        self.ent(f"port{n}_rate", "sensor", f"Switch port {n} ruch", unit_of_measurement="pkt/s",
                 state_class="measurement", icon="mdi:swap-vertical")

    def _register_node(self, name: str, master: bool, has_signal: bool) -> None:
        self.ent(f"node_{name}_conn", "sensor", f"{name} połączenie", attrs=True, icon="mdi:router-wireless")
        self.ent(f"node_{name}_online", "binary_sensor", f"{name} online", device_class="connectivity")
        if not master:
            self.ent(f"node_{name}_radio", "binary_sensor", f"{name} backhaul radiowy",
                     icon="mdi:access-point")
        if has_signal:
            self.ent(f"node_{name}_signal", "sensor", f"{name} sygnał", unit_of_measurement="dBm",
                     device_class="signal_strength", state_class="measurement")

    # -------------------------------------------------------------- zdarzenia
    def bump(self, key: str) -> None:
        self.counters[key] = int(self.counters.get(key, 0)) + 1
        self.set(f"cnt_{key}", self.counters[key])
        save_json(os.path.join(self.data_dir, "counters.json"), self.counters)

    def emit(self, etype: str, message: str, severity: str, ts: float, data: dict | None = None,
             states: dict | None = None, counters=(), delay_to: float | None = None,
             quiet: bool = False) -> None:
        data = data or {}
        if not quiet:
            for c in counters:
                self.bump(c)
        if etype.startswith("_"):
            # pojedyncze (dis)asocjacje WiFi: tylko jako szczegóły trwającego incydentu
            if not quiet and self.incidents.is_open():
                self.incidents.signal(ts, etype, message, "info", data, extend=False)
            return
        when = datetime.fromtimestamp(ts)
        self.store.add_event({"time": when.isoformat(timespec="seconds"), "type": etype, "severity": severity,
                              "message": message, "data": data, "historyczne": bool(quiet)})
        if quiet:
            return
        self.incidents.signal(ts, etype, message, severity, data)

        def publish():
            for skey, val in (states or {}).items():
                self._apply_state(skey, val)
            self.set("last_event", f"{when:%H:%M:%S} {message}"[:255],
                     {"typ": etype, "waga": severity, "czas": when.isoformat(timespec="seconds"), **data})
            payload = {"event_type": etype, "message": message, "severity": severity,
                       "time": when.isoformat(timespec="seconds"), **data}
            self.pub.state(self.ents["events"], json.dumps(payload, ensure_ascii=False, default=str),
                           retain=False)
            if self.incidents.is_open() and not self.values.get("incident_active"):
                self.set("incident_active", True)

        if delay_to and delay_to > time.time():
            self.schedule(delay_to, publish)
        else:
            publish()

    def _apply_state(self, skey: str, val) -> None:
        self.log_state_at[skey] = time.time()
        if skey.startswith("radio_backhaul:"):
            name = skey.split(":", 1)[1]
            if f"node_{name}_radio" in self.ents:
                self.set(f"node_{name}_radio", bool(val))
        elif skey in self.ents:
            self.set(skey, val)

    def _incident_closed(self, inc: dict) -> None:
        # log z tego okresu (bez szumu modemu LTE) - do panelu i atrybutów encji
        try:
            start = datetime.fromisoformat(inc["start"]).timestamp()
            end = datetime.fromisoformat(inc["end"]).timestamp()
            lines = self.store.read_range(start - 20, end + 20, max_lines=5000)
            inc["log"] = [l for l in lines if not NOISE_RE.search(l)][:150]
        except Exception:
            log.exception("Nie udało się dołączyć logu do incydentu")
        self.store.add_incident(inc)
        self.bump("incidents")
        attrs = {**inc, "log": inc.get("log", [])[:40]}
        self.set("last_incident", inc["summary"], attrs)
        self.set("incident_active", False)
        payload = {"event_type": "incident", "message": inc["summary"], "severity": "problem",
                   "time": inc["start"], "cause": inc["cause"], "duration_s": inc["duration_s"]}
        self.pub.state(self.ents["events"], json.dumps(payload, ensure_ascii=False), retain=False)
        log.warning("INCYDENT: %s", inc["summary"])

    def _ping_change(self, target, up: bool, outage) -> None:
        key = target.key
        self.set(f"ping_{key}", up)
        data = {"name": target.name, "host": target.host, "kind": target.kind}
        if not up:
            self.emit("ping_down", f"Ping {target.name} ({target.host}): brak odpowiedzi", "problem",
                      target.down_since or time.time(), data, counters=("ping_outages",))
        elif outage is not None:
            data["outage_s"] = round(outage, 1)
            self.emit("ping_up", f"Ping {target.name}: wrócił po {outage:.0f} s", "info", time.time(), data)

    # -------------------------------------------------------------- harmonogram
    def schedule(self, when: float, fn) -> None:
        with self._sched_cv:
            heapq.heappush(self._sched, (when, next(self._sched_seq), fn))
            self._sched_cv.notify()

    def _sched_loop(self) -> None:
        while True:
            with self._sched_cv:
                while not self._sched or self._sched[0][0] > time.time():
                    timeout = (self._sched[0][0] - time.time()) if self._sched else None
                    self._sched_cv.wait(timeout)
                _, _, fn = heapq.heappop(self._sched)
            try:
                fn()
            except Exception:
                log.exception("Błąd zaplanowanej publikacji")

    # -------------------------------------------------------------- pętle
    def _loop(self, name: str, interval: float, fn) -> None:
        backoff = interval
        while True:
            start = time.time()
            try:
                fn()
                backoff = interval
            except Exception:
                log.exception("Błąd w pętli %s", name)
                backoff = min(backoff * 2, 300)
            time.sleep(max(0.5, backoff - (time.time() - start)))

    def poll_syslog(self) -> None:
        try:
            text = self.deco.fetch_syslog()
        except DecoTransientError as exc:
            log.debug("%s", exc)
            return
        except DecoError as exc:
            self.status["deco"] = f"błąd: {exc}"
            log.warning("Syslog: %s", exc)
            self.deco.stok = None
            return
        lines = [l for l in text.splitlines() if l.strip()]
        new = self.dedup.new_lines(lines)
        self.status.update(deco="OK", syslog_at=datetime.now().isoformat(timespec="seconds"),
                           stale_dumps=self.dedup.stale_dumps)
        if not new:
            return
        parsed = [parse_line(l) for l in new]
        self.store.append_log([(p.time, p.raw) for p in parsed])
        now = time.time()
        # po starcie dodatku nie publikujemy starych wpisów z bufora jako nowych zdarzeń
        quiet_before = now - 120 if self._first_syslog else None
        if self._first_syslog:
            log.info("Syslog: pierwszy zrzut, %d nowych linii zapisanych do archiwum", len(new))
        self._first_syslog = False
        for p in parsed:
            for ev in self.analyzer.analyze(p):
                ts = p.time.timestamp()
                quiet = quiet_before is not None and ts < quiet_before
                self.emit(ev.type, ev.message, ev.severity, ts, ev.data, ev.states, ev.counters,
                          delay_to=ts + self.lag, quiet=quiet)
                if quiet:
                    for skey, val in ev.states.items():
                        self._apply_state(skey, val)
        self.recent_lines.extend(new[-40:])
        self.set("log_line", new[-1][:250], {"linie": list(self.recent_lines), "nowe": len(new)})

    def poll_api(self) -> None:
        d = self.deco
        perf = d.read("admin/network", "performance")
        inet = d.read("admin/network", "internet")
        wan = d.read("admin/network", "wan_ipv4")
        devs = d.read("admin/device", "device_list").get("device_list", [])
        clients = d.read("admin/client", "client_list", {"device_mac": "default"}).get("client_list", [])

        self.set("cpu", round(float(perf.get("cpu_usage", 0)) * 100))
        self.set("mem", round(float(perf.get("mem_usage", 0)) * 100))
        ip = (wan.get("wan", {}).get("ip_info") or {}).get("ip")
        if ip and ip != "0.0.0.0":
            self.set("wan_ip", ip)
        recent = time.time() - 120
        if self.log_state_at.get("wan_link", 0) < recent and inet.get("link_status"):
            self.set("wan_link", inet["link_status"] == "plugged")
        v4 = inet.get("ipv4") or {}
        if self.log_state_at.get("internet", 0) < recent and v4.get("inet_status"):
            self.set("internet", v4["inet_status"] == "online")
        self.analyzer.update_clients({c.get("mac"): b64_name(c.get("name", "")) for c in clients})
        wifi = sum(1 for c in clients if c.get("wire_type") == "wireless")
        self.set("clients_wifi", wifi)
        self.set("clients_wired", len(clients) - wifi)

        node_list = []
        for dev in devs:
            master = dev.get("role") == "master"
            name = b64_name(dev.get("custom_nickname", "")) or dev.get("nickname") or dev.get("device_model", "?")
            conn = dev.get("connection_type") or []
            wired = "wired" in conn
            if master:
                conn_text = "główny (WAN)"
            elif conn:
                conn_text = " + ".join(CONN_TEXT.get(c, c) for c in conn)
            else:
                conn_text = "radio"
            online = dev.get("group_status") == "connected"
            sig = (dev.get("signal_strength") or {}).get("band5")
            info = {
                "name": name, "master": master, "ip": dev.get("device_ip"), "model": dev.get("device_model"),
                "mac": dev.get("mac"), "wired": wired, "conn": conn_text, "online": online,
                "signal_level": dev.get("signal_level"), "signal_dbm": sig,
                "inet": dev.get("inet_status"), "fw": dev.get("software_ver"),
                "macs": [m for m in (dev.get("mac"), dev.get("bssid_2g"), dev.get("bssid_5g")) if m],
            }
            node_list.append(info)
            self._register_node(name, master, sig is not None)
            prev = self.nodes.get(name)
            now = time.time()
            if prev:
                if prev["online"] != online:
                    self.emit("node_offline" if not online else "node_online",
                              f"Węzeł {name}: {'offline' if not online else 'znów online'}",
                              "problem" if not online else "info", now, {"node": name})
                if prev["conn"] != conn_text:
                    self.emit("node_backhaul_change", f"Węzeł {name}: połączenie {prev['conn']} → {conn_text}",
                              "warning", now, {"node": name, "from": prev["conn"], "to": conn_text})
            self.nodes[name] = info
            self.set(f"node_{name}_conn", conn_text,
                     {k: v for k, v in info.items() if k not in ("macs",)})
            self.set(f"node_{name}_online", online)
            if not master and self.log_state_at.get(f"radio_backhaul:{name}", 0) < recent:
                self.set(f"node_{name}_radio", not wired)
            if sig is not None:
                self.set(f"node_{name}_signal", sig)
            if self.opts.get("ping_mesh_nodes", True) and not master and info["ip"]:
                self.add_ping_target(f"mesh_{slugify(name)}", f"Satelita {name}", info["ip"], "mesh")
        self.analyzer.update_nodes(node_list)
        self.status["deco"] = "OK"
        self._write_dashboard()

    def poll_switch(self) -> None:
        host = self.opts.get("switch_host")
        self.pinger.mark_busy(host, time.time() + 60)  # na czas odczytu (z ponownymi próbami)
        try:
            ports = self.switch.read_ports()
        except SwitchError as exc:
            self.pinger.mark_busy(host, time.time() + 2)
            self._switch_fails += 1
            if self._switch_fails == 3:  # pojedyncze odrzucenia to norma dla tego switcha
                log.warning("Switch nie odpowiada (3 odczyty z rzędu): %s", exc)
                self.status["switch"] = f"błąd: {exc}"
            return
        self.pinger.mark_busy(host, time.time() + 2)
        if self._switch_fails >= 3:
            log.info("Switch znów odpowiada")
        self._switch_fails = 0
        self.status["switch"] = "OK"
        now = time.time()
        reset = False
        for p in ports:
            n = p["port"]
            label = self.port_labels.get(n, f"port {n}")
            prev = self.port_prev.get(n)
            if prev and (p["rx_good"] < prev["rx_good"] or p["tx_good"] < prev["tx_good"]):
                reset = True
            was_up = self.port_up.get(n)
            if was_up is None or was_up != p["up"]:
                self.set(f"port{n}_link", p["up"])
            if was_up is not None and was_up != p["up"]:
                data = {"port": n, "label": label}
                if not p["up"]:
                    self.port_down_since[n] = now
                    self.emit("switch_port_down", f"Switch: {label} (port {n}) stracił link", "problem", now,
                              data, counters=("switch_link_drops",))
                else:
                    since = self.port_down_since.pop(n, None)
                    out = f" po {now - since:.0f} s" if since else ""
                    if since:
                        data["outage_s"] = round(now - since, 1)
                    self.emit("switch_port_up", f"Switch: {label} (port {n}) link wrócił{out} ({p['link']})",
                              "info", now, data)
            self.port_up[n] = p["up"]
            self.port_prev[n] = p
        if reset:
            self.emit("switch_counters_reset", "Switch: liczniki wyzerowane - restart switcha lub ręczne czyszczenie",
                      "problem", now, counters=("switch_resets",))
        if now - self.port_rate_at >= 30:
            dt = now - self.port_rate_at if self.port_rate_at else None
            for p in ports:
                n = p["port"]
                total = p["rx_good"] + p["tx_good"]
                prev_total = self.port_rate_prev.get(n)
                self.set(f"port{n}", p["link"], {"opis": self.port_labels.get(n), **p})
                self.set(f"port{n}_rxbad", p["rx_bad"])
                if dt and prev_total is not None and total >= prev_total:
                    self.port_rates[n] = round((total - prev_total) / dt, 1)
                    self.set(f"port{n}_rate", self.port_rates[n])
                self.port_rate_prev[n] = total
            self.port_rate_at = now

    def publish_stats(self) -> None:
        sample = {"t": round(time.time()), "p": {}}
        for key, st in self.pinger.stats(30).items():
            if st["samples"]:
                if st["avg"] is not None:
                    self.set(f"ping_{key}_rtt", st["avg"])
                self.set(f"ping_{key}_loss", st["loss"])
                sample["p"][key] = [st["avg"], st["max"], st["loss"]]
        for k, short in (("cpu", "cpu"), ("mem", "mem"), ("clients_wifi", "cw"), ("clients_wired", "cl")):
            if self.values.get(k) is not None:
                sample[short] = self.values[k]
        self.store.append_metric(sample)
        self.set("monitor_status", "OK" if self.status.get("deco") == "OK" else "problem",
                 {**self.status, "wersja": VERSION, "encje": len(self.ents)})

    def tick_incidents(self) -> None:
        all_up = all(t.up is not False for t in self.pinger.targets.values())
        self.incidents.tick(all_up)

    def save_persist(self) -> None:
        with self._persist_lock:
            save_json(os.path.join(self.data_dir, "state.json"), self.persist)

    def _write_dashboard(self) -> None:
        sig = (tuple(sorted(self.nodes)), tuple(sorted(self.ping_meta)), bool(self.switch))
        if sig == self._dash_sig:
            return
        self._dash_sig = sig
        try:
            import yaml

            with open(os.path.join(self.share_dir, "dashboard.yaml"), "w", encoding="utf-8") as fh:
                fh.write(build_dashboard(self))
            with open(os.path.join(self.share_dir, "dashboard-wbudowane.yaml"), "w", encoding="utf-8") as fh:
                fh.write("# Dashboard Deco Monitor tylko z wbudowanymi kartami HA (bez HACS).\n")
                fh.write(yaml.safe_dump(build_config(self, False), allow_unicode=True, sort_keys=False, width=120))
            log.info("Zapisano dashboard do %s", self.share_dir)
        except Exception:
            log.exception("Nie udało się wygenerować dashboardu")
        if self.opts.get("auto_dashboard", True) and os.environ.get("SUPERVISOR_TOKEN"):
            threading.Thread(target=self.install_dashboard, daemon=True, name="dashboard").start()

    def install_dashboard(self) -> None:
        """Tworzy/aktualizuje pulpit "Sieć Deco" w HA. Nie nadpisuje zmian zrobionych ręcznie."""
        if not self.nodes or not (self.opts.get("auto_dashboard", True) and os.environ.get("SUPERVISOR_TOKEN")):
            return
        try:
            with HomeAssistantWS() as ha:
                try:
                    resources = ha.call("lovelace/resources") or []
                except HAError:
                    resources = []
                urls = " ".join(str(r.get("url", "")) for r in resources).lower()
                hacs = "mushroom" in urls and "apexcharts" in urls
                config = build_config(self, hacs)
                new_hash = config_hash(config)
                last_hash = self.persist.get("dashboard_hash")
                dashes = ha.call("lovelace/dashboards/list") or []
                if not any(d.get("url_path") == DASH_URL for d in dashes):
                    ha.call("lovelace/dashboards/create", url_path=DASH_URL, title="Sieć Deco",
                            icon="mdi:router-network", show_in_sidebar=True, require_admin=False, mode="storage")
                    log.info("Utworzono pulpit 'Sieć Deco' w pasku bocznym HA")
                    current = None
                else:
                    try:
                        current = ha.call("lovelace/config", url_path=DASH_URL)
                    except HAError:
                        current = None
                if current:
                    cur_hash = config_hash(current)
                    if cur_hash == new_hash:
                        return
                    if cur_hash != last_hash:
                        if not self.persist.get("dashboard_edited_logged"):
                            log.info("Pulpit 'Sieć Deco' zmieniono ręcznie w HA - dodatek go nie nadpisuje "
                                     "(usuń pulpit, żeby dodatek utworzył go od nowa)")
                            self.persist["dashboard_edited_logged"] = True
                            self.save_persist()
                        return
                ha.call("lovelace/config/save", url_path=DASH_URL, config=config)
                self.persist.update(dashboard_hash=new_hash, dashboard_edited_logged=False)
                self.save_persist()
                log.info("Pulpit 'Sieć Deco' zapisany (%s)",
                         "karty Mushroom + ApexCharts" if hacs else "karty wbudowane - brak Mushroom/ApexCharts w HACS")
        except Exception as exc:
            log.warning("Nie udało się utworzyć pulpitu w HA: %s", exc)

    def snapshot(self) -> dict:
        pings = {}
        stats = self.pinger.stats(30)
        for key, meta in self.ping_meta.items():
            t = self.pinger.targets.get(key)
            pings[key] = {**meta, "up": t.up if t else None, **stats.get(key, {})}
        return {
            "version": VERSION,
            "status": self.status,
            "values": {k: self.values.get(k) for k in ("wan_link", "internet", "wan_ip", "cpu", "mem",
                                                        "clients_wifi", "clients_wired", "incident_active")},
            "nodes": [{**{k: v for k, v in n.items() if k != "macs"},
                       "radio": self.values.get(f"node_{n['name']}_radio")} for n in self.nodes.values()],
            "pings": pings,
            "ports": [{**p, "label": self.port_labels.get(n), "rate": self.port_rates.get(n)}
                      for n, p in sorted(self.port_prev.items())],
            "counters": self.counters,
            "last_incident": self.values.get("last_incident"),
        }

    # -------------------------------------------------------------- start
    def run(self) -> None:
        threading.Thread(target=self._sched_loop, daemon=True, name="sched").start()
        start_webui(self, int(os.environ.get("DECO_MONITOR_WEB_PORT", "8099")))
        log.info("Deco Monitor %s - logowanie do Deco %s", VERSION, self.opts["deco_host"])
        for attempt in range(1, 1000):
            try:
                self.deco.login()
                self.poll_api()
                break
            except Exception as exc:
                log.error("Deco niedostępne (%s), ponowna próba za 30 s", exc)
                time.sleep(30)
        log.info("Węzły mesh: %s", ", ".join(f"{n['name']} ({n['conn']})" for n in self.nodes.values()))
        self._remove_false_alarms()
        loops = [
            ("syslog", self.syslog_interval, self.poll_syslog),
            ("api", float(self.opts.get("api_interval", 30)), self.poll_api),
            ("stats", 30, self.publish_stats),
            ("incydenty", 2, self.tick_incidents),
            ("sprzątanie", 6 * 3600, self.store.cleanup),
            ("pulpit", 3600, self.install_dashboard),  # np. po doinstalowaniu kart z HACS
        ]
        if self.switch:
            loops.append(("switch", float(self.opts.get("switch_interval", 10)), self.poll_switch))
        for name, interval, fn in loops:
            threading.Thread(target=self._loop, args=(name, interval, fn), daemon=True, name=name).start()
        threading.Thread(target=self.pinger.run, daemon=True, name="ping").start()
        log.info("Monitor działa: %d encji w HA", len(self.ents))
        while True:
            time.sleep(3600)


def main() -> None:
    path = os.environ.get("DECO_MONITOR_OPTIONS", "/data/options.json")
    opts = load_json(path, None)
    if not opts:
        sys.exit(f"Brak opcji ({path})")
    logging.basicConfig(
        level=getattr(logging, str(opts.get("log_level", "info")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if not opts.get("deco_password"):
        sys.exit("Ustaw deco_password w konfiguracji dodatku.")
    Monitor(opts).run()


if __name__ == "__main__":
    main()
