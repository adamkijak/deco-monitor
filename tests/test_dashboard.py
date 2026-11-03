"""Tworzenie pulpitu w HA: atrapa serwera WebSocket API Home Assistant (lovelace/*)."""

import base64
import hashlib
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deco_monitor", "app"))

import main  # noqa: E402
from dashboard import build_config  # noqa: E402

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class FakeHA:
    """Minimalny serwer WS: auth + lovelace/resources|dashboards/list|dashboards/create|config|config/save."""

    def __init__(self, resources=()):
        self.resources = list(resources)
        self.dashboards = {}  # url_path -> config | None
        self.calls = []
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen()
        self.url = f"ws://127.0.0.1:{self.srv.getsockname()[1]}/api/websocket"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    @staticmethod
    def _recv_frame(conn, buf):
        def need(n):
            nonlocal buf
            while len(buf) < n:
                chunk = conn.recv(65536)
                if not chunk:
                    raise ConnectionError
                buf += chunk
            out, buf = buf[:n], buf[n:]
            return out
        b1, b2 = need(2)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", need(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", need(8))[0]
        mask = need(4)
        data = bytes(b ^ mask[i & 3] for i, b in enumerate(need(n)))
        return b1 & 0x0F, data, buf

    @staticmethod
    def _send(conn, obj):
        data = json.dumps(obj).encode()
        hdr = bytearray([0x81])
        if len(data) < 126:
            hdr.append(len(data))
        elif len(data) < 65536:
            hdr += bytes([126]) + struct.pack(">H", len(data))
        else:
            hdr += bytes([127]) + struct.pack(">Q", len(data))
        conn.sendall(bytes(hdr) + data)

    def _client(self, conn):
        req = b""
        while b"\r\n\r\n" not in req:
            req += conn.recv(4096)
        head, buf = req.split(b"\r\n\r\n", 1)
        key = [l.split(b": ", 1)[1] for l in head.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key")][0]
        acc = base64.b64encode(hashlib.sha1(key + GUID.encode()).digest())
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: " + acc + b"\r\n\r\n")
        self._send(conn, {"type": "auth_required"})
        try:
            while True:
                op, data, buf = self._recv_frame(conn, buf)
                if op == 0x8:
                    return
                msg = json.loads(data)
                if msg.get("type") == "auth":
                    ok = msg.get("access_token") == "TOKEN"
                    self._send(conn, {"type": "auth_ok" if ok else "auth_invalid"})
                    continue
                self.calls.append(msg["type"])
                self._send(conn, self._handle(msg))
        except ConnectionError:
            return

    def _handle(self, msg):
        t, mid = msg["type"], msg["id"]

        def ok(result=None):
            return {"id": mid, "type": "result", "success": True, "result": result}

        if t == "lovelace/resources":
            return ok([{"id": str(i), "url": u, "type": "module"} for i, u in enumerate(self.resources)])
        if t == "lovelace/dashboards/list":
            return ok([{"url_path": k, "title": "x"} for k in self.dashboards])
        if t == "lovelace/dashboards/create":
            self.dashboards[msg["url_path"]] = None
            return ok({"url_path": msg["url_path"]})
        if t == "lovelace/config":
            cfg = self.dashboards.get(msg["url_path"])
            if cfg is None:
                return {"id": mid, "type": "result", "success": False,
                        "error": {"code": "config_not_found", "message": "No config found."}}
            return ok(cfg)
        if t == "lovelace/config/save":
            self.dashboards[msg["url_path"]] = json.loads(json.dumps(msg["config"]))
            return ok()
        return {"id": mid, "type": "result", "success": False, "error": {"code": "unknown_command", "message": t}}


def make_monitor(tmp, ha_url):
    opts = {"deco_host": "127.0.0.1", "deco_password": "x", "ping_targets": [
        {"name": "Google DNS", "host": "8.8.8.8", "kind": "internet"}], "ping_mesh_nodes": False}
    os.environ.update(DECO_MONITOR_DATA=os.path.join(tmp, "data"), DECO_MONITOR_SHARE=os.path.join(tmp, "share"),
                      DECO_MONITOR_DRY_RUN="1", SUPERVISOR_TOKEN="TOKEN")
    mon = main.Monitor(opts)
    mon.nodes = {"Poddasze": {"name": "Poddasze", "master": True, "wired": False},
                 "bedroom": {"name": "bedroom", "master": False, "wired": True}}
    mon._register_node("Poddasze", True, False)
    mon._register_node("bedroom", False, True)
    main.HomeAssistantWS.__init__.__defaults__ = (ha_url, None)
    return mon


class DashboardInstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_create_builtin_then_hacs_then_respect_user_edit(self):
        ha = FakeHA()
        mon = make_monitor(self.tmp, ha.url)

        mon.install_dashboard()  # brak kart z HACS -> karty wbudowane
        cfg = ha.dashboards[main.DASH_URL]
        types = json.dumps(cfg)
        self.assertNotIn("custom:", types)
        self.assertIn("history-graph", types)
        self.assertIn("lovelace/dashboards/create", ha.calls)

        n = len(ha.calls)
        mon.install_dashboard()  # bez zmian -> bez zapisu
        self.assertNotIn("lovelace/config/save", ha.calls[n:])

        ha.resources = ["/hacsfiles/lovelace-mushroom/mushroom.js", "/hacsfiles/apexcharts-card/apexcharts-card.js"]
        mon.install_dashboard()  # doinstalowano karty -> aktualizacja do wersji z HACS
        self.assertIn("custom:mushroom-template-card", json.dumps(ha.dashboards[main.DASH_URL]))

        ha.dashboards[main.DASH_URL]["title"] = "Mój pulpit"  # użytkownik edytuje w HA
        ha.resources = []
        mon.install_dashboard()
        self.assertEqual(ha.dashboards[main.DASH_URL]["title"], "Mój pulpit")  # nie nadpisano

    def test_builtin_variant_has_no_custom_cards(self):
        ha = FakeHA()
        mon = make_monitor(self.tmp, ha.url)
        self.assertNotIn("custom:", json.dumps(build_config(mon, False)))
        self.assertIn("custom:apexcharts-card", json.dumps(build_config(mon, True)))


if __name__ == "__main__":
    unittest.main()
