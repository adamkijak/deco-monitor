"""Obecność domowników po nazwie klienta na liście Deco (np. "OnePlus-13").

Dopasowanie po nazwie, nie po MAC: Android używa losowego MAC-a dla sieci, który zmienia się po
"zapomnij sieć" albo resecie ustawień, a nazwa (hostname telefonu) zostaje ta sama.
"""

from __future__ import annotations

import time

BANDS = {"band2_4": "2,4 GHz", "band5": "5 GHz", "band6": "6 GHz"}


class Presence:
    """`not_home` dopiero po `away_after` s nieobecności na liście (Android w uśpieniu potrafi
    na chwilę zniknąć z WiFi). Zanim tracker przejdzie w `not_home`, `ping(ip)` sprawdza ostatni znany
    adres telefonu: odpowiedź przedłuża obecność o kolejne `away_after`. Pingujemy tylko do
    `ping_window` s od ostatniego widzenia na liście, bo potem Deco mogło dać ten IP komuś innemu.
    Brak odczytu z Deco nie zmienia stanu - update() wołamy tylko po udanym pobraniu listy klientów.

    Noc (`night_names`, godziny `night_hours` "HH:MM-HH:MM", czas lokalny): telefon, który był `home`,
    zostaje `home` do końca okna, nawet gdy zniknie z listy i nie odpowiada na ping (uśpiony telefon
    potrafi wyłączyć WiFi na ponad godzinę). Przyjście w nocy działa normalnie. Po końcu okna wraca
    zwykła reguła (`away_after` + ping)."""

    def __init__(self, names, away_after: float = 300, ping_window: float = 1800,
                 night_names=None, night_hours: str = "23:00-07:00"):
        self.names = [n.strip() for n in names or [] if n and n.strip()]
        self.away_after = float(away_after)
        self.ping_window = float(ping_window)
        self.night = {n.strip().casefold() for n in night_names or [] if n and n.strip()}
        self.night_start, self.night_end = parse_hours(night_hours)
        self.last_seen: dict = {}  # nazwa -> czas ostatniego widzenia na liście
        self.ping_ok: dict = {}  # nazwa -> czas ostatniej odpowiedzi na ping (poza listą)
        self.info: dict = {}  # nazwa -> atrybuty z ostatniego widzenia
        self.state: dict = {}  # nazwa -> ostatni zwrócony stan (do trzymania `home` w nocy)

    def is_night(self, now: float) -> bool:
        t = time.localtime(now)
        m = t.tm_hour * 60 + t.tm_min
        if self.night_start <= self.night_end:
            return self.night_start <= m < self.night_end
        return m >= self.night_start or m < self.night_end

    # -- zapis między restartami dodatku ------------------------------------
    def export(self) -> dict:
        return {"last_seen": dict(self.last_seen), "ping_ok": dict(self.ping_ok), "info": dict(self.info),
                "state": dict(self.state)}

    def load(self, data: dict | None) -> None:
        data = data or {}
        for attr in ("last_seen", "ping_ok", "info", "state"):
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
            night_hold = (not home and c is None and n.casefold() in self.night
                          and self.state.get(n) == "home" and self.is_night(now))
            if night_hold:
                home = True
            self.state[n] = "home" if home else "not_home"
            attrs = {"nazwa_w_deco": n, "na_liscie": c is not None, **self.info.get(n, {})}
            if seen is not None and c is None:  # tylko gdy zniknął - stałe atrybuty = mniej zapisów w recorderze
                attrs["ostatnio_widziany"] = seen
                if n in self.ping_ok:
                    attrs["odpowiada_na_ping"] = self.ping_ok[n]
                if night_hold:
                    attrs["noc_zakladamy_dom"] = True
            out[n] = ("home" if home else "not_home", attrs)
        return out


def parse_hours(spec: str) -> tuple:
    """"23:00-07:00" -> (1380, 420) w minutach od północy."""
    try:
        a, b = (x.strip() for x in str(spec).split("-"))
        return tuple(int(x.split(":")[0]) * 60 + int(x.split(":")[1]) for x in (a, b))
    except (ValueError, IndexError):
        return (23 * 60, 7 * 60)
