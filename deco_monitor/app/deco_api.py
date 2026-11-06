"""Klient lokalnego API TP-Link Deco (ten sam protokół co aplikacja i panel WWW).

RSA + AES /cgi-bin/luci, logowanie jako "admin" hasłem administratora.
Protokół: https://github.com/roquerodrigo/tplink-deco-api
"""

from __future__ import annotations

import json
import math
import re
import secrets
import ssl
import threading
import urllib.error
import urllib.request
from base64 import b64decode, b64encode
from hashlib import md5
from urllib.parse import quote_plus

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

USERNAME = "admin"
_COOKIE_RE = re.compile(r"(sysauth=[a-f0-9]+)")


class DecoError(RuntimeError):
    pass


class DecoTransientError(DecoError):
    """Router odpowiedział, ale chwilowo nie dał danych (np. errorcode timeout)."""


def _rsa_encrypt(n: int, e: int, plaintext: bytes) -> str:
    k = (int(math.log2(n)) + 8) >> 3
    out = ""
    for i in range(0, len(plaintext), k - 11):
        block = plaintext[i : i + k - 11]
        pad = b""
        while len(pad) < k - len(block) - 3:
            b = secrets.token_bytes(1)
            if b != b"\x00":
                pad += b
        em = b"\x00\x02" + pad + b"\x00" + block
        out += format(pow(int.from_bytes(em, "big"), e, n), f"0{k * 2}x")
    return out


def _aes(key: str, iv: str):
    return Cipher(algorithms.AES(key.encode()), modes.CBC(iv.encode()), backend=default_backend())


def _aes_encrypt(key: str, iv: str, text: str) -> str:
    p = sym_padding.PKCS7(128).padder()
    data = p.update(text.encode()) + p.finalize()
    enc = _aes(key, iv).encryptor()
    return b64encode(enc.update(data) + enc.finalize()).decode()


def _aes_decrypt(key: str, iv: str, b64: str) -> str:
    dec = _aes(key, iv).decryptor()
    data = dec.update(b64decode(b64)) + dec.finalize()
    u = sym_padding.PKCS7(128).unpadder()
    return (u.update(data) + u.finalize()).decode()


class DecoClient:
    """Jedna sesja administratora, współdzielona przez wątki (blokada)."""

    def __init__(self, host: str, password: str, timeout: float = 20.0, scheme: str = "https",
                 legacy_tls: bool = False):
        self.host = host
        self.password = password
        self.timeout = timeout
        # Satelity starszej generacji (M4R, M3W) mają panel tylko po HTTP albo z TLS 1.0,
        # którego domyślny kontekst Pythona nie przyjmuje. Hasło i tak idzie zaszyfrowane RSA/AES.
        self.scheme = scheme
        self._lock = threading.RLock()
        self._cookie = None
        self._ctx = ssl.create_default_context()
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE
        if legacy_tls:
            try:
                self._ctx.minimum_version = ssl.TLSVersion.TLSv1
                self._ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
            except (ValueError, ssl.SSLError):
                pass
        self.stok = None
        self._key = self._iv = None
        self._hash = None
        self._seq = 0
        self._sign = None

    # -- transport -------------------------------------------------------
    def _http(self, url: str, data: bytes, headers: dict) -> bytes:
        headers = dict(headers)
        if self._cookie:
            headers["Cookie"] = self._cookie
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                for h, v in resp.headers.items():
                    if h.lower() == "set-cookie":
                        m = _COOKIE_RE.search(v)
                        if m:
                            self._cookie = m.group(1)
                return resp.read()
        except urllib.error.HTTPError as exc:
            raise DecoError(f"HTTP {exc.code} {url.split('?')[0].split('/')[-1]}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise DecoError(f"Brak połączenia z Deco: {getattr(exc, 'reason', exc)}") from exc

    def _post_json(self, url: str, body) -> dict:
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        raw = self._http(url, data, {"Content-Type": "application/json"})
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise DecoError(f"Niepoprawna odpowiedź Deco: {raw[:120]!r}") from exc

    def _login_url(self, form: str) -> str:
        return f"{self.scheme}://{self.host}/cgi-bin/luci/;stok=/login?form={form}"

    def _admin_url(self, path: str, form: str) -> str:
        return f"{self.scheme}://{self.host}/cgi-bin/luci/;stok={self.stok}/{path}?form={form}"

    # -- sesja -----------------------------------------------------------
    def login(self) -> None:
        with self._lock:
            self._cookie = None
            auth = self._post_json(self._login_url("auth"), {"operation": "read"}).get("result", {})
            keys = self._post_json(self._login_url("keys"), {"operation": "read"}).get("result", {})
            sk, pk = auth.get("key", []), keys.get("password", [])
            if len(sk) < 2 or len(pk) < 2:
                raise DecoError("Nieudane logowanie: brak kluczy RSA z routera")
            self._sign = (int(sk[0], 16), int(sk[1], 16))
            self._seq = int(auth.get("seq", 0))
            self._key = str(secrets.randbelow(9 * 10**15) + 10**15)
            self._iv = str(secrets.randbelow(9 * 10**15) + 10**15)
            self._hash = md5((USERNAME + self.password).encode()).hexdigest()
            pwd = _rsa_encrypt(int(pk[0], 16), int(pk[1], 16), self.password.encode())
            raw = self._post_json(
                self._login_url("login"),
                self._payload({"operation": "login", "params": {"password": pwd}}),
            )
            stok = self._decode(raw).get("stok")
            if not stok:
                raise DecoError("Router odrzucił logowanie (złe hasło?)")
            self.stok = stok

    def _payload(self, data: dict) -> str:
        b64 = _aes_encrypt(self._key, self._iv, json.dumps(data, separators=(",", ":")))
        sig = f"k={self._key}&i={self._iv}&h={self._hash}&s={self._seq + len(b64)}"
        return f"sign={_rsa_encrypt(self._sign[0], self._sign[1], sig.encode())}&data={quote_plus(b64)}"

    def _decode(self, raw: dict) -> dict:
        if not raw.get("data"):
            raise DecoError(f"Błąd API {raw.get('error_code', raw.get('errorcode', '?'))}")
        decoded = json.loads(_aes_decrypt(self._key, self._iv, raw["data"]))
        code = decoded.get("error_code") or decoded.get("errorcode")
        if code:
            raise DecoError(f"Błąd API {code}")
        return decoded.get("result", {})

    def _ensure_login(self) -> None:
        if not self.stok:
            self.login()

    def request(self, path: str, form: str, data: dict, _retry: bool = True) -> dict:
        with self._lock:
            self._ensure_login()
            try:
                raw = self._post_json(self._admin_url(path, form), self._payload(data))
                return self._decode(raw)
            except DecoError:
                if not _retry:
                    raise
                # Najczęstsza przyczyna: wygasła sesja albo inna aplikacja
                # (np. integracja tplink_deco) zalogowała się w międzyczasie.
                self.login()
                return self.request(path, form, data, _retry=False)

    # -- wywołania -------------------------------------------------------
    def read(self, path: str, form: str, params: dict | None = None) -> dict:
        body = {"operation": "read"}
        if params is not None:
            body["params"] = params
        return self.request(path, form, body)

    def fetch_syslog(self) -> str:
        """Zrzut bufora syslogu routera - dokładnie jak przycisk "Eksportuj log"
        w panelu WWW: najpierw build (odświeża snapshot), potem save_log."""
        with self._lock:
            self.request("admin/log_export", "feedback_log", {"operation": "build"})
            boundary = "----deco_monitor_boundary"
            body = (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="operation"\r\n\r\n'
                f"save\r\n--{boundary}--\r\n"
            ).encode()
            headers = {
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Origin": f"{self.scheme}://{self.host}",
                "Referer": f"{self.scheme}://{self.host}/webpages/index.html",
            }
            text = self._http(self._admin_url("admin/log_export", "save_log"), body, headers).decode(
                "utf-8", errors="replace"
            )
        head = text.lstrip()[:200]
        if head.startswith("{") and '"success"' in head:
            # np. { "success":false, "errorcode":"timeout" } - router nie zdążył
            if "timeout" in head:
                raise DecoTransientError("Router nie zdążył przygotować logu (timeout)")
            self.stok = None
            raise DecoError(f"Eksport logu odrzucony: {head[:80]}")
        return text


def b64_name(value: str) -> str:
    if not value:
        return ""
    try:
        return b64decode(value).decode("utf-8").strip()
    except Exception:
        return value
