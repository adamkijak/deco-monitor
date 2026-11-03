"""Składanie pojedynczych sygnałów (log, ping, switch, API) w incydenty.

Incydent otwiera każdy sygnał o wadze "problem". Zamyka się, gdy wszystkie
pingi są OK i przez `quiet_s` nie przyszedł żaden nowy problem (logi Deco
docierają z opóźnieniem, więc quiet_s musi być dłuższe niż to opóźnienie).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


class IncidentTracker:
    def __init__(self, quiet_s: float, on_close):
        self.quiet_s = quiet_s
        self.on_close = on_close
        self._lock = threading.Lock()
        self._open: dict | None = None

    def signal(self, ts: float, etype: str, text: str, severity: str, data: dict | None = None) -> None:
        with self._lock:
            if self._open is None:
                if severity != "problem":
                    return
                self._open = {"start": ts, "items": [], "last_problem": ts, "last_signal": time.time()}
            inc = self._open
            inc["items"].append({"ts": ts, "type": etype, "text": text, "severity": severity,
                                 "data": data or {}})
            inc["last_signal"] = time.time()
            if severity == "problem":
                inc["last_problem"] = max(inc["last_problem"], ts)
            inc["start"] = min(inc["start"], ts)

    def is_open(self) -> bool:
        return self._open is not None

    def tick(self, all_pings_up: bool) -> None:
        with self._lock:
            inc = self._open
            if inc is None or not all_pings_up:
                return
            if time.time() - inc["last_signal"] < self.quiet_s:
                return
            self._open = None
        self.on_close(summarize(inc))


def summarize(inc: dict) -> dict:
    items = sorted(inc["items"], key=lambda i: i["ts"])
    types = [i["type"] for i in items]
    tset = set(types)
    start = items[0]["ts"]
    end = items[-1]["ts"]
    duration = max(0, round(end - start))

    def pings(kind=None, down=True):
        return [i for i in items if i["type"] == ("ping_down" if down else "ping_up")
                and (kind is None or i["data"].get("kind") == kind)]

    if "wan_link_down" in tset:
        n = types.count("wan_link_down")
        cause = "Zanik kabla WAN" + (f" (×{n})" if n > 1 else "")
    elif "router_boot" in tset:
        cause = "Restart głównego Deco"
    elif "failover_to_mobile" in tset:
        cause = "Przełączenie na LTE (łącze zapasowe)"
    elif "internet_offline" in tset or pings("internet"):
        cause = "Brak internetu (kabel WAN OK)"
    elif pings("local"):
        cause = "Sieć lokalna niedostępna: " + ", ".join(sorted({i["data"].get("name", "?") for i in pings("local")}))
    elif "switch_port_down" in tset:
        cause = "Utrata linku na switchu"
    elif pings("mesh"):
        cause = "Satelita niedostępna: " + ", ".join(sorted({i["data"].get("name", "?") for i in pings("mesh")}))
    elif "backhaul_radio_start" in tset:
        cause = "Satelita straciła kabel"
    elif "clients_kicked" in tset or "wifi_mass_disconnect" in tset:
        cause = "Masowe rozłączenie klientów WiFi"
    elif "node_offline" in tset:
        cause = "Węzeł Deco offline"
    else:
        cause = items[0]["text"]

    effects = []
    if "clients_kicked" in tset:
        effects.append(f"Deco wyrzuciło klientów WiFi ×{types.count('clients_kicked')}")
    if "wifi_mass_disconnect" in tset and "clients_kicked" not in tset:
        effects.append("masowe rozłączenia WiFi")
    if "lan_dhcp_restart" in tset:
        effects.append(f"restart DHCP LAN ×{types.count('lan_dhcp_restart')}")
    if "eth_ports_reset" in tset:
        effects.append("reset portów Ethernet")
    # tylko satelity, które zgubiły kabel (te zawsze radiowe mają wagę "warning")
    radio = sorted({i["data"].get("node") for i in items
                    if i["type"] == "backhaul_radio_start" and i["severity"] == "problem"} - {None})
    if radio and cause != "Satelita straciła kabel":
        effects.append("na radiu: " + ", ".join(radio))
    unreachable = sorted({i["data"].get("node") for i in items if i["type"] == "mesh_node_unreachable"} - {None})
    if unreachable:
        effects.append("satelity niedostępne: " + ", ".join(unreachable))
    down = sorted({i["data"].get("name") for i in pings()} - {None})
    if down:
        effects.append("brak pingu: " + ", ".join(down))
    if "switch_port_down" in tset and not cause.startswith("Utrata linku"):
        ports = sorted({str(i["data"].get("label")) for i in items if i["type"] == "switch_port_down"})
        effects.append("switch: " + ", ".join(ports))

    summary = f"{_fmt(start)} {cause}, {duration} s"
    if effects:
        summary += "; " + "; ".join(effects)
    return {
        "start": datetime.fromtimestamp(start).isoformat(timespec="seconds"),
        "end": datetime.fromtimestamp(end).isoformat(timespec="seconds"),
        "duration_s": duration,
        "cause": cause,
        "effects": effects,
        "summary": summary[:250],
        "timeline": [
            {"time": datetime.fromtimestamp(i["ts"]).strftime("%H:%M:%S"), "type": i["type"],
             "text": i["text"], "severity": i["severity"]}
            for i in items
        ],
    }
