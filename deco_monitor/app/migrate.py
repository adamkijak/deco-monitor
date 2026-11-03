"""Jednorazowe usunięcie fałszywych alarmów zapisanych przez wersje < 1.2.0.

Wersje 1.0-1.1 analizowały te same linie logu kilka razy (nieaktualne zrzuty bufora),
przez co powstawały alarmy "masowe rozłączenia WiFi", których log nie potwierdza.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime

_STA_DISC = re.compile(r"hostapd: ath\d+: AP-STA-DISCONNECTED ([0-9a-fA-F:]{17})")


def _read(path: str) -> list:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def _write(path: str, items: list) -> None:
    if os.path.exists(path) and not os.path.exists(path + ".przed-1.2.1"):
        os.replace(path, path + ".przed-1.2.1")  # kopia na wszelki wypadek
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for it in items:
            fh.write(json.dumps(it, ensure_ascii=False, default=str) + "\n")
    os.replace(tmp, path)


def remove_false_alarms(store, mass_devices: int, satellite_tails: set) -> dict:
    from syslog_parser import mac_tail

    stats = {"usuniete_zdarzenia": 0, "usuniete_incydenty": 0}
    events = _read(store.events_path)
    kept, seen, removed_keys = [], set(), set()
    for e in events:
        key = (e.get("time"), e.get("type"), e.get("message"))
        if key in seen:  # dokładna kopia tego samego zdarzenia
            stats["usuniete_zdarzenia"] += 1
            continue
        seen.add(key)
        if e.get("type") == "wifi_mass_disconnect":
            try:
                t = datetime.fromisoformat(e["time"]).timestamp()
                lines = store.read_range(t - 60, t + 1, max_lines=20000)
            except (KeyError, ValueError):
                lines = []
            if lines:  # bez logu nie da się sprawdzić - zostawiamy
                macs = {m.group(1).lower() for l in lines for m in [_STA_DISC.search(l)] if m}
                macs = {m for m in macs if mac_tail(m) not in satellite_tails}
                if len(macs) < mass_devices:
                    removed_keys.add((e["time"][:10], e["time"][11:19]))
                    stats["usuniete_zdarzenia"] += 1
                    continue
        kept.append(e)
    if len(kept) != len(events):
        _write(store.events_path, kept)

    incidents = _read(store.incidents_path)
    kept_inc, seen_inc = [], set()
    for inc in incidents:
        key = (inc.get("start"), inc.get("summary"))
        day = (inc.get("start") or "")[:10]
        problems = [x for x in inc.get("timeline", []) if x.get("severity") == "problem"]
        only_false = problems and all(x.get("type") == "wifi_mass_disconnect" and (day, x.get("time")) in removed_keys
                                      for x in problems)
        if key in seen_inc or only_false:
            stats["usuniete_incydenty"] += 1
            continue
        seen_inc.add(key)
        kept_inc.append(inc)
    if len(kept_inc) != len(incidents):
        _write(store.incidents_path, kept_inc)
    return stats
