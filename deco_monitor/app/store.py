"""Archiwum na dysku: surowe logi Deco (plik na dzień) + zdarzenia/incydenty (JSONL)."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime

from noise import is_important  # noqa: E402

_DAY_RE = re.compile(r"^deco-(\d{4}-\d{2}-\d{2})\.log$")


class Store:
    def __init__(self, root: str, retention_days: int):
        self.root = root
        self.logs_dir = os.path.join(root, "logs")
        os.makedirs(self.logs_dir, exist_ok=True)
        self.retention_days = retention_days
        self.events_path = os.path.join(root, "events.jsonl")
        self.incidents_path = os.path.join(root, "incidents.jsonl")
        self.metrics_dir = os.path.join(root, "metrics")
        os.makedirs(self.metrics_dir, exist_ok=True)
        self._lock = threading.Lock()

    # -- surowy log --------------------------------------------------------
    def log_path(self, day: str) -> str:
        return os.path.join(self.logs_dir, f"deco-{day}.log")

    def append_log(self, entries: list) -> None:
        """entries: [(datetime|None, raw_line)]"""
        if not entries:
            return
        by_day: dict = {}
        today = datetime.now().strftime("%Y-%m-%d")
        for ts, raw in entries:
            day = ts.strftime("%Y-%m-%d") if ts else today
            by_day.setdefault(day, []).append(raw)
        with self._lock:
            for day, lines in by_day.items():
                with open(self.log_path(day), "a", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")

    def tail_lines(self, max_lines: int = 30000) -> list:
        """Ostatnie linie archiwum (wczoraj + dziś) - do odbudowy deduplikacji po starcie."""
        out: list = []
        for day in sorted(self.days())[-2:]:
            with open(self.log_path(day), encoding="utf-8", errors="replace") as fh:
                out.extend(l.rstrip("\n") for l in fh if l.strip())
        return out[-max_lines:]

    def read_range(self, from_ts: float, to_ts: float, max_lines: int = 3000) -> list:
        """Linie archiwum z przedziału czasu (wg znacznika czasu routera)."""
        from syslog_parser import line_epoch

        out, inside = [], False
        day = datetime.fromtimestamp(from_ts).replace(hour=0, minute=0, second=0, microsecond=0)
        while day.timestamp() <= to_ts:
            path = self.log_path(day.strftime("%Y-%m-%d"))
            if os.path.exists(path):
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.rstrip("\n")
                        if not line:
                            continue
                        ts = line_epoch(line)
                        if ts is not None:
                            inside = from_ts <= ts <= to_ts
                        if inside:  # linie bez czasu (kontynuacje) idą z poprzednią
                            out.append(line)
                            if len(out) >= max_lines:
                                return out
            day = datetime.fromtimestamp(day.timestamp() + 86400 + 3600).replace(hour=0)
        return out

    def dedupe_archive(self) -> int:
        """Jednorazowe usunięcie duplikatów z archiwum zapisanego przez wersje < 1.2.0."""
        from syslog_parser import LineDeduper

        removed = 0
        with self._lock:
            for day in sorted(self.days()):
                path = self.log_path(day)
                with open(path, encoding="utf-8", errors="replace") as fh:
                    lines = [l.rstrip("\n") for l in fh if l.strip()]
                clean = LineDeduper().new_lines(lines)
                if len(clean) < len(lines):
                    removed += len(lines) - len(clean)
                    os.replace(path, path + ".przed-1.2.0")
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write("\n".join(clean) + "\n")
        return removed

    def days(self) -> list:
        out = []
        for name in os.listdir(self.logs_dir):
            m = _DAY_RE.match(name)
            if m:
                out.append(m.group(1))
        return sorted(out, reverse=True)

    def read_log(self, day: str, query: str = "", levels: set | None = None, limit: int = 3000,
                 important: bool = False) -> dict:
        path = self.log_path(day)
        if not _DAY_RE.match(os.path.basename(path)) or not os.path.exists(path):
            return {"lines": [], "total": 0, "matched": 0}
        q = query.lower().strip()
        total, matched = 0, []
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line:
                    continue
                total += 1
                if q and q not in line.lower():
                    continue
                if levels:
                    lvl = _level_of(line)
                    if lvl not in levels:
                        continue
                if important and not is_important(line):
                    continue
                matched.append(line)
        return {"lines": matched[-limit:], "total": total, "matched": len(matched)}

    # -- JSONL ---------------------------------------------------------------
    def _append_jsonl(self, path: str, obj: dict) -> None:
        with self._lock, open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")

    def _tail_jsonl(self, path: str, limit: int) -> list:
        if not os.path.exists(path):
            return []
        with self._lock, open(path, encoding="utf-8") as fh:
            lines = fh.readlines()[-limit:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return list(reversed(out))

    def add_event(self, obj: dict) -> None:
        self._append_jsonl(self.events_path, obj)

    def add_incident(self, obj: dict) -> None:
        self._append_jsonl(self.incidents_path, obj)

    def events(self, limit: int = 500) -> list:
        return self._tail_jsonl(self.events_path, limit)

    def incidents(self, limit: int = 200) -> list:
        return self._tail_jsonl(self.incidents_path, limit)

    def events_since(self, since_ts: float) -> list:
        """Zdarzenia od podanego czasu, chronologicznie."""
        since = datetime.fromtimestamp(since_ts).isoformat(timespec="seconds")
        return [e for e in reversed(self.events(200000)) if e.get("time", "") >= since]

    def incidents_since(self, since_ts: float) -> list:
        since = datetime.fromtimestamp(since_ts).isoformat(timespec="seconds")
        return [i for i in reversed(self.incidents(20000)) if i.get("end", "") >= since]

    # -- pomiary (do wykresów w panelu) -------------------------------------------
    def append_metric(self, sample: dict) -> None:
        day = datetime.fromtimestamp(sample["t"]).strftime("%Y-%m-%d")
        path = os.path.join(self.metrics_dir, f"metrics-{day}.jsonl")
        with self._lock, open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(sample, separators=(",", ":")) + "\n")

    def metrics_since(self, since_ts: float) -> list:
        out = []
        day = datetime.fromtimestamp(since_ts).replace(hour=0, minute=0, second=0, microsecond=0)
        while day.timestamp() <= time.time():
            path = os.path.join(self.metrics_dir, f"metrics-{day:%Y-%m-%d}.jsonl")
            if os.path.exists(path):
                with self._lock, open(path, encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            m = json.loads(line)
                        except ValueError:
                            continue
                        if m.get("t", 0) >= since_ts:
                            out.append(m)
            day = datetime.fromtimestamp(day.timestamp() + 86400 + 3600).replace(hour=0)
        return out

    # -- sprzątanie ------------------------------------------------------------
    def cleanup(self) -> None:
        cutoff = time.time() - self.retention_days * 86400
        for name in os.listdir(self.metrics_dir):
            m = re.match(r"^metrics-(\d{4}-\d{2}-\d{2})\.jsonl$", name)
            try:
                if m and datetime.strptime(m.group(1), "%Y-%m-%d").timestamp() < cutoff:
                    os.remove(os.path.join(self.metrics_dir, name))
            except OSError:
                pass
        for day in self.days():
            try:
                if datetime.strptime(day, "%Y-%m-%d").timestamp() < cutoff:
                    os.remove(self.log_path(day))
                    if os.path.exists(self.log_path(day) + ".przed-1.2.0"):
                        os.remove(self.log_path(day) + ".przed-1.2.0")
            except (OSError, ValueError):
                pass
        for path, keep in ((self.events_path, 50000), (self.incidents_path, 5000)):
            if os.path.exists(path) and os.path.getsize(path) > 8 * 1024 * 1024:
                with self._lock:
                    with open(path, encoding="utf-8") as fh:
                        lines = fh.readlines()[-keep:]
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.writelines(lines)


_LVL_RE = re.compile(r"^\S+ \S+ +\d+ \S+ \d{4} [a-z]+\.([a-z]+) ")


def _level_of(line: str) -> str:
    m = _LVL_RE.match(line)
    return m.group(1) if m else ""
