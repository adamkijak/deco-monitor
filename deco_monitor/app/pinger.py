"""Ping co N sekund do listy celów, z wykrywaniem zaników i statystyką okna."""

from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass, field

_TIME_RE = re.compile(r"time[=<]([\d.]+) ?ms")


def ping_once(host: str, timeout_s: int = 1) -> float | None:
    try:
        r = subprocess.run(["ping", "-n", "-c", "1", "-W", str(timeout_s), host],
                           capture_output=True, text=True, timeout=timeout_s + 2)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    m = _TIME_RE.search(r.stdout)
    return float(m.group(1)) if m else 0.0


@dataclass
class Target:
    key: str
    name: str
    host: str
    kind: str  # internet | local | mesh
    up: bool | None = None
    fails: int = 0
    down_since: float | None = None
    first_fail: float | None = None
    window: list = field(default_factory=list)  # (czas, rtt|None)


class Pinger:
    """Wywołuje on_change(target, up, outage_seconds) przy zmianie stanu
    i udostępnia stats() dla okna statystyk."""

    def __init__(self, interval: float, fail_threshold: int, on_change):
        self.interval = interval
        self.fail_threshold = max(1, fail_threshold)
        self.on_change = on_change
        self.targets: dict = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        # host -> czas, do którego nie mierzymy (np. switch, gdy dodatek czyta jego panel WWW:
        # ten sam słaby procesor odpowiada na ping i generuje stronę - pomiar mierzyłby sam siebie)
        self.busy_until: dict = {}

    def mark_busy(self, host: str, until: float) -> None:
        self.busy_until[host] = until

    def _busy(self, host: str) -> bool:
        return time.time() < self.busy_until.get(host, 0)

    def set_target(self, key: str, name: str, host: str, kind: str) -> bool:
        with self._lock:
            t = self.targets.get(key)
            if t and t.host == host:
                return False
            self.targets[key] = Target(key, name, host, kind)
            return True

    def _probe(self, t: Target) -> None:
        if self._busy(t.host):
            return
        rtt = ping_once(t.host)
        now = time.time()
        if self._busy(t.host):  # odczyt panelu zaczął się w trakcie pingu - próbka niemiarodajna
            return
        with self._lock:
            t.window.append((now, rtt))
            if rtt is not None:
                t.fails = 0
                if t.up is not True:
                    outage = now - t.down_since if t.down_since else None
                    was_known = t.up is not None
                    t.up, t.down_since = True, None
                    changed = (True, outage if was_known else None)
                else:
                    changed = None
            else:
                t.fails += 1
                if t.fails == 1:
                    t.first_fail = now
                if t.up is not False and t.fails >= self.fail_threshold:
                    t.up = False
                    t.down_since = t.first_fail or now
                    changed = (False, None)
                else:
                    changed = None
        if changed:
            self.on_change(t, changed[0], changed[1])

    def stats(self, window_s: float) -> dict:
        """key -> {avg, max, loss, samples}; czyści stare próbki."""
        cutoff = time.time() - window_s
        out = {}
        with self._lock:
            for t in self.targets.values():
                t.window = [w for w in t.window if w[0] >= cutoff]
                rtts = [r for _, r in t.window if r is not None]
                n = len(t.window)
                out[t.key] = {
                    "avg": round(sum(rtts) / len(rtts), 1) if rtts else None,
                    "max": round(max(rtts), 1) if rtts else None,
                    "loss": round(100.0 * (n - len(rtts)) / n, 1) if n else None,
                    "samples": n,
                }
        return out

    def run(self) -> None:
        while not self._stop.is_set():
            start = time.time()
            with self._lock:
                targets = list(self.targets.values())
            threads = [threading.Thread(target=self._probe, args=(t,), daemon=True) for t in targets]
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=5)
            self._stop.wait(max(0.2, self.interval - (time.time() - start)))

    def stop(self) -> None:
        self._stop.set()
