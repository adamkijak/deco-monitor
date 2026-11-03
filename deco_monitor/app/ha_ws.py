"""Minimalny klient WebSocket API Home Assistant (bez zewnętrznych bibliotek).

W dodatku łączy się przez Supervisora: ws://supervisor/core/websocket z tokenem
SUPERVISOR_TOKEN (wymaga `homeassistant_api: true` w config.yaml).
"""

from __future__ import annotations

import base64
import itertools
import json
import os
import socket
import ssl
import struct
from urllib.parse import urlparse


class HAError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


class WebSocket:
    def __init__(self, url: str, timeout: float = 20.0):
        u = urlparse(url)
        port = u.port or (443 if u.scheme == "wss" else 80)
        sock = socket.create_connection((u.hostname, port), timeout)
        if u.scheme == "wss":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.hostname}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("Serwer zamknął połączenie podczas handshake")
            resp += chunk
        head, self._buf = resp.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0]
        if b" 101" not in status:
            raise ConnectionError(f"Handshake WebSocket odrzucony: {status.decode(errors='replace')}")
        self.sock = sock

    def _frame(self, opcode: int, data: bytes) -> None:
        hdr = bytearray([0x80 | opcode])
        n = len(data)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
        self.sock.sendall(bytes(hdr) + masked)

    def send(self, text: str) -> None:
        self._frame(0x1, text.encode())

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("Połączenie WebSocket zamknięte")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self) -> str:
        msg = b""
        while True:
            b1, b2 = self._read(2)
            fin, opcode, n = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            payload = self._read(n)
            if mask:
                payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
            if opcode == 0x9:  # ping
                self._frame(0xA, payload)
                continue
            if opcode == 0x8:
                raise ConnectionError("Serwer zamknął połączenie WebSocket")
            if opcode in (0x0, 0x1, 0x2):
                msg += payload
                if fin:
                    return msg.decode("utf-8")

    def close(self) -> None:
        try:
            self._frame(0x8, b"")
            self.sock.close()
        except OSError:
            pass


class HomeAssistantWS:
    def __init__(self, url: str = "ws://supervisor/core/websocket", token: str | None = None):
        self.url = url
        self.token = token or os.environ.get("SUPERVISOR_TOKEN")
        self._ids = itertools.count(1)
        self.ws = None

    def __enter__(self):
        if not self.token:
            raise HAError("no_token", "Brak SUPERVISOR_TOKEN")
        self.ws = WebSocket(self.url)
        first = json.loads(self.ws.recv())
        if first.get("type") != "auth_required":
            raise HAError("auth", f"Nieoczekiwana odpowiedź: {first}")
        self.ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        res = json.loads(self.ws.recv())
        if res.get("type") != "auth_ok":
            raise HAError("auth", res.get("message", "odrzucono token"))
        return self

    def __exit__(self, *exc):
        if self.ws:
            self.ws.close()

    def call(self, type_: str, **params):
        msg_id = next(self._ids)
        self.ws.send(json.dumps({"id": msg_id, "type": type_, **params}))
        while True:
            res = json.loads(self.ws.recv())
            if res.get("id") != msg_id or res.get("type") != "result":
                continue
            if not res.get("success"):
                err = res.get("error") or {}
                raise HAError(err.get("code", "error"), err.get("message", ""))
            return res.get("result")
