"""Panel WWW dodatku (HA ingress): przegląd z wykresami, incydenty, zdarzenia, archiwum logów."""

from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import viz
from noise import NOISE_RE

log = logging.getLogger("webui")

# Ingress Supervisora łączy się z tego adresu; lokalnie (testy) dopuszczamy localhost.
ALLOWED = {"172.30.32.2", "127.0.0.1", "::1"}
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def start_webui(mon, port: int) -> None:
    store = mon.store
    open_all = os.environ.get("DECO_MONITOR_WEB_OPEN") == "1"

    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj):
            self._send(200, json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json")

        def do_GET(self):
            if not open_all and self.client_address[0] not in ALLOWED:
                return self._send(403, b"forbidden", "text/plain")
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            parts = u.path.rstrip("/").rsplit("/api/", 1)
            route = parts[1] if len(parts) == 2 else ""
            hours = min(max(float(q.get("hours", 24) or 24), 0.25), 24 * 31) if "hours" in q else 24.0
            try:
                if route == "status":
                    return self._json(mon.snapshot())
                if route == "timeline":
                    start = float(q["from"]) if q.get("from") else None
                    end = float(q["to"]) if q.get("to") else None
                    return self._json(viz.timeline(store, mon, hours, start, end, q.get("compact") == "1"))
                if route == "metrics":
                    return self._json(viz.metrics(store, hours))
                if route == "daily":
                    return self._json(viz.daily(store, min(int(q.get("days", 14)), 90)))
                if route == "log_range":
                    lines = store.read_range(float(q["from"]), float(q["to"]), 6000)
                    total = len(lines)
                    if q.get("noise") != "1":
                        lines = [l for l in lines if not NOISE_RE.search(l)]
                    return self._json({"lines": lines, "total": total})
                if route == "days":
                    return self._json(store.days())
                if route == "log":
                    levels = {x for x in q.get("levels", "").split(",") if x} or None
                    return self._json(store.read_log(q.get("day", ""), q.get("q", ""), levels,
                                                     min(int(q.get("limit", 3000)), 20000)))
                if route == "events":
                    return self._json(store.events(min(int(q.get("limit", 500)), 5000)))
                if route == "incidents":
                    return self._json(store.incidents(min(int(q.get("limit", 100)), 2000)))
                if route == "download":
                    day = q.get("day", "")
                    r = store.read_log(day, limit=10**9)
                    body = ("\n".join(r["lines"]) + "\n").encode()
                    return self._send(200, body, "text/plain; charset=utf-8",
                                      {"Content-Disposition": f'attachment; filename="deco-{day}.log"'})
                if route == "" and not u.path.rstrip("/").endswith(".ico"):
                    with open(os.path.join(WEB_DIR, "index.html"), "rb") as fh:
                        return self._send(200, fh.read(), "text/html; charset=utf-8")
                return self._send(404, b"not found", "text/plain")
            except (ValueError, OSError) as exc:
                log.warning("Panel: %s %s", self.path, exc)
                return self._send(400, str(exc).encode(), "text/plain")
            except Exception:
                log.exception("Panel: błąd %s", self.path)
                return self._send(500, b"internal error", "text/plain")

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True, name="webui").start()
    log.info("Panel WWW na porcie %d", port)
