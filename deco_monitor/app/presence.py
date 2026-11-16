"""Obecność domowników po nazwie klienta na liście Deco (np. "OnePlus-13").

Dopasowanie po nazwie, nie po MAC: Android używa losowego MAC-a dla sieci, który zmienia się po
"zapomnij sieć" albo resecie ustawień, a nazwa (hostname telefonu) zostaje ta sama.
"""

from __future__ import annotations

BANDS = {"band2_4": "2,4 GHz", "band5": "5 GHz", "band6": "6 GHz"}


class Presence:
    """`not_home` dopiero po `away_after` s nieobecności na liście (Android w uśpieniu potrafi
    na chwilę zniknąć z WiFi). Zanim tracker przejdzie w `not_home`, `ping(ip)` sprawdza ostatni znany
    adres telefonu: odpowiedź przedłuża obecność o kolejne `away_after`. Pingujemy tylko do
    `ping_window` s od ostatniego widzenia na liście, bo potem Deco mogło dać ten IP komuś innemu.
    Brak odczytu z Deco nie zmienia stanu - update() wołamy tylko po udanym pobraniu listy klientów."""

    def __init__(self, names, away_after: float = 300, ping_window: float = 1800):
        self.names = [n.strip() for n in names or [] if n and n.strip()]
        self.away_after = float(away_after)
        self.ping_window = float(ping_window)
        self.last_seen: dict = {}  # nazwa -> czas ostatniego widzenia na liście
        self.ping_ok: dict = {}  # nazwa -> czas ostatniej odpowiedzi na ping (poza listą)
        self.info: dict = {}  # nazwa -> atrybuty z ostatniego widzenia

    # -- zapis między restartami dodatku ------------------------------------
    def export(self) -> dict:
        return {"last_seen": dict(self.last_seen), "ping_ok": dict(self.ping_ok), "info": dict(self.info)}

    def load(self, data: dict | None) -> None:
        data = data or {}
        for attr in ("last_seen", "ping_ok", "info"):
            saved = data.get(attr) or {}
            getattr(self, attr).update({n: v for n, v in saved.items() if n in self.names})

    # -----------------------------------------------------------------------
    def update(self, clients: list, now: float, ping=None) -> dict:
        """clients: [{"name": <zdekodowana nazwa>, "mac", "ip", "connection_type", "online"}, ...]
        ping: funkcja ip -> bool (opcjonalna).
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
                self.ping_ok.pop(n, None)
                self.info[n] = {
                    "ip": c.get("ip"),
                    "mac": c.get("mac"),
                    "pasmo": BANDS.get(c.get("connection_type"), c.get("connection_type")),
                    "polaczenie": c.get("wire_type"),
                }
            seen = self.last_seen.get(n)
            evidence = max(seen or 0, self.ping_ok.get(n, 0)) or None
            home = evidence is not None and now - evidence < self.away_after
            ip = self.info.get(n, {}).get("ip")
            if (not home and c is None and seen is not None and ping and ip
                    and now - seen < self.ping_window and ping(ip)):
                self.ping_ok[n] = now
                home = True
            attrs = {"nazwa_w_deco": n, "na_liscie": c is not None, **self.info.get(n, {})}
            if seen is not None and c is None:  # tylko gdy zniknął - stałe atrybuty = mniej zapisów w recorderze
                attrs["ostatnio_widziany"] = seen
                if n in self.ping_ok:
                    attrs["odpowiada_na_ping"] = self.ping_ok[n]
            out[n] = ("home" if home else "not_home", attrs)
        return out
