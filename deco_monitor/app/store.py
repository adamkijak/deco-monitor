"""Archiwum na dysku: surowe logi Deco (plik na dzień) + zdarzenia/incydenty (JSONL)."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime

_DAY_RE = re.compile(r"^deco-(\d{4}-\d{2}-\d{2})\.log$")


class Store:
    def __init__(self, root: str, retention_days: int):
        self.root = root
        self.logs_dir = os.path.join(root, "logs")
        os.makedirs(self.logs_dir, exist_ok=True)
        self.retention_days = retention_days
        self.events_path = os.path.join(root, "events.jsonl")
        self.incidents_path = os.path.join(root, "incidents.jsonl")
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

    def days(self) -> list:
        out = []
        for name in os.listdir(self.logs_dir):
            m = _DAY_RE.match(name)
            if m:
                out.append(m.group(1))
        return sorted(out, reverse=True)

    def read_log(self, day: str, query: str = "", levels: set | None = None, limit: int = 3000) -> dict:
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

    # -- sprzątanie ------------------------------------------------------------
    def cleanup(self) -> None:
        cutoff = time.time() - self.retention_days * 86400
        for day in self.days():
            try:
                if datetime.strptime(day, "%Y-%m-%d").timestamp() < cutoff:
                    os.remove(self.log_path(day))
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
