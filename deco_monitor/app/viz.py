"""Dane do wykresów panelu: pasy osi czasu, serie pomiarów, statystyka dzienna."""

from __future__ import annotations

import time
from collections import OrderedDict
from datetime import datetime, timedelta


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


# typ zdarzenia otwierającego -> (typ zamykającego, pas, waga, etykieta)
PAIRS = {
    "internet_offline": ("internet_online", "Internet (Deco)", "problem", "brak internetu"),
    "wan_link_down": ("wan_link_up", "Kabel WAN", "problem", "brak linku WAN"),
    "failover_to_mobile": ("failover_to_wan", "Łącze LTE", "warning", "praca na LTE"),
}
POINTS = {
    "clients_kicked": "Deco wyrzuciło klientów WiFi",
    "eth_ports_reset": "reset portów Ethernet",
    "lan_dhcp_restart": "restart DHCP",
    "wifi_mass_disconnect": "masowe rozłączenia WiFi",
    "router_boot": "restart Deco",
    "switch_counters_reset": "restart switcha",
}


def _ping_lane(name: str, kind: str | None) -> str:
    # pingi do satelit trafiają na pas danej satelity ("Satelita bedroom")
    return name if kind == "mesh" else f"Ping {name}"


def timeline(store, mon, hours: float, start: float | None = None, end: float | None = None,
             compact: bool = False) -> dict:
    """Pasy osi czasu. start/end - wycinek (np. jeden incydent); compact - tylko pasy z aktywnością."""
    now = end or time.time()
    since = start if start is not None else now - hours * 3600
    events = [e for e in store.events_since(since - 6 * 3600) if e.get("time", "") <=
              datetime.fromtimestamp(now).isoformat(timespec="seconds")]  # zapas: otwarcia sprzed okna
    lanes: "OrderedDict[str, dict]" = OrderedDict()

    def lane(name: str, group: str) -> dict:
        if name not in lanes:
            lanes[name] = {"name": name, "group": group, "intervals": [], "points": []}
        return lanes[name]

    lane("Incydenty", "inc")
    lane("Internet (Deco)", "wan")
    lane("Kabel WAN", "wan")
    lane("Łącze LTE", "lte")
    for key, meta in mon.ping_meta.items():
        if meta["kind"] != "mesh":
            lane(f"Ping {meta['name']}", meta["kind"])
    for name, info in sorted(mon.nodes.items(), key=lambda kv: kv[0]):
        if not info["master"]:
            lane(f"Satelita {name}", "mesh")
    lane("Zdarzenia Deco", "dev")

    open_: dict = {}

    def close(key, end, lname, group, sev, label):
        start = open_.pop(key, None)
        if start is not None:
            lane(lname, group)["intervals"].append([start, end, label, sev])

    for e in events:
        try:
            t = _ts(e["time"])
        except (KeyError, ValueError):
            continue
        typ, data = e.get("type"), e.get("data") or {}
        if typ in PAIRS:
            open_.setdefault(typ, t)
        for opener, (closer, lname, sev, label) in PAIRS.items():
            if typ == closer:
                close(opener, t, lname, "wan", sev, label)
        if typ == "ping_down":
            open_.setdefault(("ping", data.get("name")), t)
        elif typ == "ping_up":
            close(("ping", data.get("name")), t, _ping_lane(data.get("name"), data.get("kind")),
                  data.get("kind", "local"), "problem", "brak odpowiedzi na ping")
        elif typ == "backhaul_radio_start":
            node = data.get("node")
            if mon.nodes.get(node, {}).get("wired"):
                open_.setdefault(("radio", node), t)
        elif typ == "backhaul_radio_end":
            node = data.get("node")
            close(("radio", node), t, f"Satelita {node}", "mesh", "warning", "na radiu zamiast kabla")
        elif typ == "mesh_node_unreachable":
            lane(f"Satelita {data.get('node')}", "mesh")["points"].append([t, "niedostępna dla głównego Deco",
                                                                            "warning"])
        elif typ == "node_offline":
            open_.setdefault(("node", data.get("node")), t)
        elif typ == "node_online":
            close(("node", data.get("node")), t, f"Satelita {data.get('node')}", "mesh", "problem", "offline")
        elif typ == "switch_port_down":
            open_.setdefault(("port", data.get("port")), t)
        elif typ == "switch_port_up":
            n = data.get("port")
            close(("port", n), t, f"Switch port {n} ({data.get('label', '')})", "switch", "problem", "brak linku")
        elif typ in POINTS:
            lane("Zdarzenia Deco", "dev")["points"].append([t, POINTS[typ], e.get("severity", "warning")])

    # wciąż otwarte: do teraz, jeśli stan nadal zły; inaczej krótki odcinek
    for key, start in list(open_.items()):
        still_bad = False
        if isinstance(key, tuple) and key[0] == "ping":
            still_bad = any(m["name"] == key[1] and mon.pinger.targets[k].up is False
                            for k, m in mon.ping_meta.items() if k in mon.pinger.targets)
            kind = next((m["kind"] for m in mon.ping_meta.values() if m["name"] == key[1]), "local")
            close(key, now if still_bad else start + 2, _ping_lane(key[1], kind), kind, "problem",
                  "brak odpowiedzi na ping")
        elif key == "wan_link_down":
            still_bad = mon.values.get("wan_link") is False
            close(key, now if still_bad else start + 2, "Kabel WAN", "wan", "problem", "brak linku WAN")
        elif key == "internet_offline":
            still_bad = mon.values.get("internet") is False
            close(key, now if still_bad else start + 2, "Internet (Deco)", "wan", "problem", "brak internetu")
        else:
            open_.pop(key, None)

    for inc in store.incidents_since(since):
        try:
            s, e = _ts(inc["start"]), _ts(inc["end"])
        except (KeyError, ValueError):
            continue
        lanes["Incydenty"]["intervals"].append([s, max(e, s + 1), inc.get("summary", ""), "problem"])

    for ln in lanes.values():
        ln["intervals"] = [i for i in ln["intervals"] if i[1] >= since]
        ln["points"] = [p for p in ln["points"] if p[0] >= since]
    for ln in lanes.values():
        ln["intervals"] = [[max(i[0], since), min(i[1], now)] + i[2:] for i in ln["intervals"]]
    # porty switcha i LTE: tylko gdy coś się działo; w trybie compact - wszystkie puste pasy znikają
    out = [ln for ln in lanes.values()
           if (ln["group"] not in ("switch", "lte") or ln["intervals"])
           and (not compact or ln["intervals"] or ln["points"] or ln["name"] in ("Internet (Deco)", "Kabel WAN"))]
    return {"from": since, "to": now, "lanes": out}


def metrics(store, hours: float, max_points: int = 400) -> dict:
    now = time.time()
    since = now - hours * 3600
    rows = store.metrics_since(since)
    bucket = max(30.0, hours * 3600 / max_points)
    series: dict = {}
    sys_series = {"cpu": [], "mem": [], "cw": [], "cl": []}
    buckets: "OrderedDict[int, list]" = OrderedDict()
    for r in rows:
        buckets.setdefault(int(r["t"] // bucket), []).append(r)
    for b, items in buckets.items():
        t = (b + 0.5) * bucket
        per: dict = {}
        for r in items:
            for key, (avg, mx, loss) in (r.get("p") or {}).items():
                acc = per.setdefault(key, [[], [], []])
                if avg is not None:
                    acc[0].append(avg)
                if mx is not None:
                    acc[1].append(mx)
                if loss is not None:
                    acc[2].append(loss)
        for key, (avgs, mxs, losses) in per.items():
            series.setdefault(key, []).append([
                round(t), round(sum(avgs) / len(avgs), 1) if avgs else None,
                max(mxs) if mxs else None, max(losses) if losses else None,
            ])
        for k in sys_series:
            vals = [r[k] for r in items if r.get(k) is not None]
            if vals:
                sys_series[k].append([round(t), round(sum(vals) / len(vals), 1)])
    return {"from": since, "to": now, "bucket": bucket, "ping": series, "sys": sys_series}


def daily(store, days: int) -> list:
    start = (datetime.now() - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    keys = {"incidents": 0, "wan_link_down": 0, "internet_offline": 0, "ping_internet": 0,
            "backhaul_radio_start": 0, "clients_kicked": 0, "wifi_mass_disconnect": 0}
    out = OrderedDict(((start + timedelta(days=i)).strftime("%Y-%m-%d"), dict(keys)) for i in range(days))
    for e in store.events_since(start.timestamp()):
        day = e.get("time", "")[:10]
        if day not in out:
            continue
        typ = e.get("type")
        if typ == "ping_down" and (e.get("data") or {}).get("kind") == "internet":
            out[day]["ping_internet"] += 1
        elif typ in keys:
            out[day][typ] += 1
    for inc in store.incidents_since(start.timestamp()):
        day = inc.get("start", "")[:10]
        if day in out:
            out[day]["incidents"] += 1
    return [{"day": d, **v} for d, v in out.items()]
