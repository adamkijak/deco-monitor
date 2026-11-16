"""Obecność domowników po nazwie klienta na liście Deco (np. "OnePlus-13").

Dopasowanie po nazwie, nie po MAC: Android używa losowego MAC-a dla sieci, który zmienia się po
"zapomnij sieć" albo resecie ustawień, a nazwa (hostname telefonu) zostaje ta sama.
"""

from __future__ import annotations

BANDS = {"band2_4": "2,4 GHz", "band5": "5 GHz", "band6": "6 GHz"}


class Presence:
    """`not_home` dopiero po `away_after` s nieobecności na liście (Android w uśpieniu potrafi
    na chwilę zniknąć z WiFi). Brak odczytu z Deco nie zmienia stanu - update() wołamy tylko
    po udanym pobraniu listy klientów."""

    def __init__(self, names, away_after: float = 300):
        self.names = [n.strip() for n in names or [] if n and n.strip()]
        self.away_after = float(away_after)
        self.last_seen: dict = {}  # nazwa -> czas ostatniego widzenia
        self.info: dict = {}  # nazwa -> atrybuty z ostatniego widzenia

    def update(self, clients: list, now: float) -> dict:
        """clients: [{"name": <zdekodowana nazwa>, "mac", "ip", "connection_type", "online"}, ...]
        Zwraca {nazwa: (stan, atrybuty)} dla wszystkich obserwowanych nazw."""
        found = {}
        for c in clients:
            if c.get("online") is False:
                continue
            name = (c.get("name") or "").strip().casefold()
            if name:
                found.setdefault(name, c)
        out = {}
        for n in self.names:
            c = found.get(n.casefold())
            if c is not None:
                self.last_seen[n] = now
                self.info[n] = {
                    "ip": c.get("ip"),
                    "mac": c.get("mac"),
                    "pasmo": BANDS.get(c.get("connection_type"), c.get("connection_type")),
                    "polaczenie": c.get("wire_type"),
                }
            seen = self.last_seen.get(n)
            home = seen is not None and now - seen < self.away_after
            attrs = {"nazwa_w_deco": n, "na_liscie": c is not None, **self.info.get(n, {})}
            if seen is not None and c is None:  # tylko gdy zniknął - stałe atrybuty = mniej zapisów w recorderze
                attrs["ostatnio_widziany"] = seen
            out[n] = ("home" if home else "not_home", attrs)
        return out
