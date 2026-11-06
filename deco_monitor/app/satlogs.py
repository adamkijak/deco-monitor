"""Archiwum syslogu satelitów Deco.

Log głównego Deco zawiera tylko jego własne radia - co dzieje się po drugiej stronie
radiowego backhaulu (np. kuchnia) widać tylko w logu samej satelity. Satelity przyjmują
to samo hasło administratora i ten sam eksport logu co główny Deco, ale:
  * starsze modele (M4R) mają panel po zwykłym HTTP, a HTTPS tylko z TLS 1.0,
  * niektóre (M3W 1.0.x) odrzucają logowanie (HTTP 403) - takie sprawdzamy rzadko,
  * bufor jest mały (~40 min przy ruchliwym 2,4 GHz), więc pobieramy co kilka minut.
Logi trafiają do /share/deco_monitor/logs/deco-<satelita>-RRRR-MM-DD.log.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from deco_api import DecoClient, DecoError, DecoTransientError
from mqtt_pub import slugify
from syslog_parser import LineDeduper, parse_line

log = logging.getLogger("satlogs")

# kolejność prób połączenia z satelitą: (schemat, stary TLS)
MODES = (("http", False), ("https", True))
BLOCKED_RETRY = 6 * 3600  # satelita odrzucił logowanie (403) - kolejna próba za 6 h


class _Sat:
    def __init__(self, name: str, source: str, host: str):
        self.name = name
        self.source = source
        self.host = host
        self.client = None
        self.mode = None
        self.dedup = None
        self.next_try = 0.0
        self.status = "czeka"
        self.last_ok = None
        self.lines = 0


class SatelliteLogs:
    def __init__(self, store, password: str, interval: float, client_factory=DecoClient, clock=time.time):
        self.store = store
        self.password = password
        self.interval = interval
        self._factory = client_factory
        self._clock = clock
        self.sats: dict = {}  # źródło -> _Sat

    def update_nodes(self, nodes: list) -> None:
        """nodes: lista węzłów z poll_api (name, ip, master)."""
        for n in nodes:
            if n.get("master") or not n.get("ip"):
                continue
            source = slugify(n["name"]) or "satelita"
            sat = self.sats.get(source)
            if sat is None:
                self.sats[source] = _Sat(n["name"], source, n["ip"])
            elif sat.host != n["ip"]:
                sat.host, sat.client, sat.next_try = n["ip"], None, 0.0

    def names(self) -> dict:
        return {s.source: s.name for s in self.sats.values()}

    def status(self) -> dict:
        return {s.name: s.status for s in self.sats.values()}

    def poll(self) -> None:
        for sat in list(self.sats.values()):
            if self._clock() < sat.next_try:
                continue
            try:
                self._poll_one(sat)
            except Exception:
                log.exception("Log satelity %s", sat.name)
                sat.client = None

    # ------------------------------------------------------------------
    def _connect(self, sat: _Sat) -> bool:
        errors = []
        for scheme, legacy in MODES:
            client = self._factory(sat.host, self.password, scheme=scheme, legacy_tls=legacy)
            try:
                client.login()
            except DecoError as exc:
                errors.append(f"{scheme}: {exc}")
                continue
            if sat.mode != scheme:
                log.info("Satelita %s (%s): log dostępny przez %s", sat.name, sat.host, scheme.upper())
            sat.client, sat.mode = client, scheme
            return True
        msg = "; ".join(errors)
        if any("HTTP 403" in e for e in errors):
            # satelita działa, ale nie wpuszcza (np. M3W) - nie męczymy go co kilka minut
            if not sat.status.startswith("brak dostępu"):
                log.info("Satelita %s (%s) nie udostępnia logu (%s) - kolejna próba za 6 h",
                         sat.name, sat.host, msg)
            sat.status = "brak dostępu (satelita odrzuca logowanie)"
            sat.next_try = self._clock() + BLOCKED_RETRY
        else:
            log.debug("Satelita %s (%s) niedostępna: %s", sat.name, sat.host, msg)
            sat.status = f"niedostępna: {msg}"[:200]
        return False

    def _poll_one(self, sat: _Sat) -> None:
        if sat.client is None and not self._connect(sat):
            return
        try:
            text = sat.client.fetch_syslog()
        except DecoTransientError:
            return
        except DecoError as exc:
            # sesja wygasła albo satelita się restartuje - przy następnym cyklu łączymy od nowa
            sat.client = None
            sat.status = f"błąd: {exc}"[:200]
            return
        if sat.dedup is None:
            sat.dedup = LineDeduper()
            sat.dedup.remember(self.store.tail_lines(source=sat.source))
        lines = [l for l in text.splitlines() if l.strip()]
        new = sat.dedup.new_lines(lines)
        if new:
            parsed = [parse_line(l) for l in new]
            self.store.append_log([(p.time, p.raw) for p in parsed], source=sat.source)
            sat.lines += len(new)
        sat.last_ok = datetime.fromtimestamp(self._clock()).isoformat(timespec="seconds")
        sat.status = f"OK ({sat.mode.upper()}, ostatnio {sat.last_ok[11:]})"
