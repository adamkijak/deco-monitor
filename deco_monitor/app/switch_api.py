"""Odczyt stanu portów TP-Link Easy Smart (TL-SG108E / TL-SG105E i podobne).

Tylko odczyt: logowanie przez /logon.cgi, potem strony PortSettingRpm.htm
i PortStatisticsRpm.htm, z których parsowany jest obiekt JS `all_info`.
"""

from __future__ import annotations

import http.cookiejar
import re
import threading
import time
import urllib.parse
import urllib.request

LINK_TEXT = {0: "brak linku", 1: "auto", 2: "10 Mb/s half", 3: "10 Mb/s", 4: "100 Mb/s half",
             5: "100 Mb/s", 6: "1000 Mb/s"}
LINK_MBPS = {0: 0, 2: 10, 3: 10, 4: 100, 5: 100, 6: 1000}

_ARR_RE = re.compile(r"(\w+):\[([^\]]*)\]")


class SwitchError(RuntimeError):
    pass


def _parse_all_info(html: str) -> dict:
    m = re.search(r"var all_info\s*=\s*\{(.*?)\};", html, re.S)
    if not m:
        raise SwitchError("Brak danych all_info na stronie switcha")
    out = {}
    for key, body in _ARR_RE.findall(m.group(1)):
        vals = [v.strip() for v in body.split(",") if v.strip()]
        out[key] = [int(v, 0) for v in vals]
    return out


class EasySmartSwitch:
    def __init__(self, host: str, username: str, password: str, timeout: float = 8.0):
        self.base = f"http://{host}"
        self.username = username
        self.password = password
        self.timeout = timeout
        self._lock = threading.Lock()
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))
        self._logged_in = False
        self.port_count = 8

    def _get(self, path: str, data: bytes | None = None) -> str:
        req = urllib.request.Request(self.base + path, data=data, headers={"Referer": self.base + "/"})
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return resp.read().decode("gb2312", errors="replace")
        except OSError as exc:
            raise SwitchError(f"Brak połączenia ze switchem: {exc}") from exc

    def login(self) -> None:
        body = urllib.parse.urlencode(
            {"username": self.username, "password": self.password, "cpassword": "", "logon": "Login"}
        ).encode()
        html = self._get("/logon.cgi", body)
        m = re.search(r"logonInfo = new Array\(\s*(\d+)", html)
        if m and m.group(1) not in ("0",):
            raise SwitchError(f"Switch odrzucił logowanie (kod {m.group(1)})")
        self._logged_in = True

    def _page(self, name: str) -> dict:
        if not self._logged_in:
            self.login()
        html = self._get(f"/{name}.htm")
        if "logonInfo" in html and "all_info" not in html:
            # sesja wygasła - strona logowania
            self.login()
            html = self._get(f"/{name}.htm")
        return _parse_all_info(html)

    def read_ports(self) -> list:
        """Lista portów: {port, enabled, link_code, link, mbps, tx_good, tx_bad, rx_good, rx_bad}."""
        with self._lock:
            # serwer WWW switcha jest słaby: zrywa połączenia, gdy dostaje kilka żądań naraz
            # (np. ktoś ma otwarty jego panel) - do 3 prób z przerwą
            for attempt in range(3):
                try:
                    stats = self._page("PortStatisticsRpm")
                    break
                except SwitchError:
                    if attempt == 2:
                        raise
                    self._logged_in = False
                    time.sleep(1.0 + attempt)
        state = stats.get("state", [])
        link = stats.get("link_status", [])
        pkts = stats.get("pkts", [])
        n = min(self.port_count, len(link))
        ports = []
        for i in range(n):
            code = link[i]
            p = pkts[4 * i : 4 * i + 4] + [0, 0, 0, 0]
            ports.append({
                "port": i + 1,
                "enabled": bool(state[i]) if i < len(state) else True,
                "link_code": code,
                "link": LINK_TEXT.get(code, str(code)),
                "mbps": LINK_MBPS.get(code, 0),
                "up": code != 0,
                "tx_good": p[0], "tx_bad": p[1], "rx_good": p[2], "rx_bad": p[3],
            })
        return ports
