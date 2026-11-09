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


class FakeDeco:
    def __init__(self):
        self.devs = []

    def read(self, path, form, params=None):
        if form == "device_list":
            return {"device_list": self.devs}
        return {}


def node(name, role="slave", conn=("wired",), speed="100"):
    d = {"nickname": name, "role": role, "group_status": "connected", "connection_type": list(conn),
         "device_ip": None, "mac": name}
    if speed is not None:
        d["backhual_speed"] = speed
    return d


class BackhaulSpeedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_parse_speed(self):
        self.assertEqual(main.parse_speed("100"), 100)
        self.assertEqual(main.parse_speed("10"), 10)
        self.assertIsNone(main.parse_speed(None))
        self.assertIsNone(main.parse_speed("0"))
        self.assertIsNone(main.parse_speed("abc"))

    def test_sensor_event_and_dashboard(self):
        mon = make_monitor(self.tmp, FakeHA().url)
        mon.nodes = {}
        mon.deco = FakeDeco()
        mon.satlogs = None
        events = []
        mon.emit = lambda etype, msg, sev, ts, data=None, **kw: events.append((etype, sev, data))
        mon.deco.devs = [node("Poddasze", role="master", conn=(), speed=None), node("living_room"),
                         node("kitchen", conn=("band5",), speed="1000")]
        mon.poll_api()
        self.assertEqual(mon.values["node_living_room_bh_speed"], 100)
        self.assertNotIn("node_kitchen_bh_speed", mon.ents)  # radio: brak prędkości kabla
        self.assertNotIn("node_Poddasze_bh_speed", mon.ents)
        self.assertEqual(events, [])

        mon.deco.devs[1]["backhual_speed"] = "10"
        mon.poll_api()
        self.assertEqual(mon.values["node_living_room_bh_speed"], 10)
        self.assertEqual(events, [("node_backhaul_speed", "warning",
                                   {"node": "living_room", "from": 100, "to": 10})])

        mon.deco.devs[1]["connection_type"] = ["band5"]  # utrata kabla -> 0 na wykresie, bez zdarzenia prędkości
        mon.poll_api()
        self.assertEqual(mon.values["node_living_room_bh_speed"], 0)
        self.assertEqual([ev[0] for ev in events[1:]], ["node_backhaul_change"])

        cfg = json.dumps(build_config(mon, False), ensure_ascii=False)
        self.assertIn("Prędkość kabla do satelit", cfg)
        self.assertIn(mon.ents["node_living_room_bh_speed"], cfg)
        self.assertIn("node_backhaul_speed", main.EVENT_TYPES)


class ParentTopologyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    @staticmethod
    def devs():
        m = node("Poddasze", role="master", conn=(), speed=None)
        m["mac"] = "7C-F1-7E-69-FA-08"
        lr = node("living_room")
        lr.update(device_id="LR", parent_device_id="MASTER-ID", mac="B8-FB-B3-AA-A1-80")
        k = node("kitchen", conn=("band5",), speed=None)
        k.update(device_id="K", parent_device_id="LR", mac="00-31-92-29-CA-B4")
        h = node("hallway", conn=(), speed=None)  # M3W: bez parent_device_id, rodzic w "previous"
        h.update(device_id="H", previous="7c:f1:7e:69:fa:08", mac="90-9A-4A-B2-CB-EC")
        return [m, lr, k, h]

    def test_resolve_parents(self):
        p = main.resolve_parents(self.devs(), lambda d: d["nickname"])
        self.assertEqual(p, {"living_room": "Poddasze", "kitchen": "living_room", "hallway": "Poddasze"})
        lone = node("x", conn=())
        self.assertEqual(main.resolve_parents([lone], lambda d: d["nickname"]), {"x": None})

    def test_sensor_event_topology_dashboard(self):
        mon = make_monitor(self.tmp, FakeHA().url)
        mon.nodes = {}
        mon.deco = FakeDeco()
        mon.satlogs = None
        events = []
        mon.emit = lambda etype, msg, sev, ts, data=None, **kw: events.append((etype, sev, data))
        mon.deco.devs = self.devs()
        mon.poll_api()
        self.assertEqual(mon.values["node_kitchen_parent"], "living_room")
        self.assertEqual(mon.values["node_hallway_parent"], "Poddasze")
        self.assertNotIn("node_Poddasze_parent", mon.ents)
        self.assertIn("Poddasze: hallway, living_room", mon.values["mesh_topology"])
        self.assertIn("living_room: kitchen", mon.values["mesh_topology"])
        self.assertEqual(events, [])

        mon.deco.devs[2]["parent_device_id"] = "MASTER-ID"  # kuchnia przeskakuje na Poddasze
        mon.poll_api()
        self.assertEqual(mon.values["node_kitchen_parent"], "Poddasze")
        self.assertEqual(events, [("node_parent_change", "warning",
                                   {"node": "kitchen", "from": "living_room", "to": "Poddasze"})])
        self.assertIn("node_parent_change", main.EVENT_TYPES)

        cfg = json.dumps(build_config(mon, False), ensure_ascii=False)
        self.assertIn("Topologia mesh", cfg)
        self.assertIn(mon.ents["mesh_topology"], cfg)
        hacs = json.dumps(build_config(mon, True), ensure_ascii=False)  # kafelki mushroom: "→ rodzic"
        self.assertIn(f"→ {{{{ states('{mon.ents['node_kitchen_parent']}') }}}}", hacs)

    def test_topology_tree(self):
        nodes = {
            "Poddasze": {"master": True, "online": True},
            "living_room": {"parent": "Poddasze", "conn": "kabel", "bh_speed": 100, "online": True},
            "kitchen": {"parent": "living_room", "conn": "radio 5 GHz", "signal_dbm": -70, "online": False},
            "ghost": {"parent": "nieistniejący", "conn": "radio", "online": True},
        }
        short, tree = main.topology_text(nodes)
        lines = tree.split("  \n")
        self.assertTrue(lines[0].startswith("**Poddasze** – główne Deco"))
        self.assertIn("**ghost**", tree)  # nieznany rodzic -> pod głównym
        kitchen = next(ln for ln in lines if "kitchen" in ln)
        self.assertIn("&nbsp;" * 8, kitchen)
        self.assertIn("-70 dBm", kitchen)
        self.assertIn("**offline**", kitchen)
        self.assertIn("kabel 100 Mbit/s", tree)
        self.assertTrue(short.startswith("Poddasze: ghost, living_room"))


if __name__ == "__main__":
    unittest.main()
