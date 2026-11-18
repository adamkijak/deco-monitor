"""Odczyt stanu adaptera MoCA 2.5 na układzie MaxLinear (np. Kiwee KB-M3-02).

Tylko odczyt, tymi samymi zapytaniami co strony devStatus.html i phyRates.html panelu:
POST /ms/<warstwa>/<id> z treścią {"data":[...]}, Basic auth i nagłówkiem X-CSRF-TOKEN
(token z ciasteczka `csrf_token`, które panel ustawia przy pobraniu dowolnej strony).

Adapter ma stały adres (domyślnie 192.168.144.200, bez DHCP), więc host, z którego pytamy,
musi mieć adres w tej samej podsieci.
"""

from __future__ import annotations

import base64
import http.cookiejar
import json
import threading
import urllib.error
import urllib.request

MAX_NODES = 16
# PHY rate wg phyRates.html (MoCA 2.x, 100 MHz): 3900 * bity_na_symbol / ((512 + (CP + 10) * 2) * 46)
LDPC_LEN = 3900
FFT_LEN = 512


class MocaError(RuntimeError):
    pass


class MocaUnreachable(MocaError):
    """Brak połączenia (np. host nie ma adresu w podsieci adaptera)."""


def fmt_mac(hi: int, lo: int) -> str:
    raw = f"{hi & 0xffffffff:08x}{(lo >> 16) & 0xffff:04x}"
    return ":".join(raw[i:i + 2] for i in range(0, 12, 2))


def _ver(v: int) -> str:
    return f"{(v >> 4) & 0xf}.{v & 0xf}"


def _u64(vals: list, i: int) -> int:
    return (vals[i] & 0xffffffff) * 4294967296 + (vals[i + 1] & 0xffffffff)


def parse_local(vals: list) -> dict:
    return {
        "node_id": vals[0] & 0xff,
        "nc_id": vals[1] & 0xff,
        "link_up": vals[5] != 0,
        "net_version": _ver(vals[11]),
        "node_mask": vals[12],
    }


def parse_frames(vals: list) -> dict:
    """Liczniki ramek portu Ethernet adaptera (devStatus.html, /ms/0/0x14)."""
    return {
        "tx_good": _u64(vals, 12), "tx_bad": _u64(vals, 30), "tx_dropped": _u64(vals, 48),
        "rx_good": _u64(vals, 66), "rx_bad": _u64(vals, 84), "rx_dropped": _u64(vals, 102),
    }


def parse_fmr(fmr: list, mask: int, node_ver: int, nc_ver: int, net_vers: dict) -> dict:
    """Odpowiedź FMR jednego węzła -> {węzeł_docelowy: (nper, vlper)} w Mb/s.
    Przepisane z phyRates.html (tylko gałąź MoCA 2.x; 1.x zwraca 0)."""
    out = {}
    ri = 10
    aligned = True
    for j in range(MAX_NODES):
        if not mask >> j & 1:
            if node_ver >= 0x20:
                ri += 1 if aligned else 2
            elif not aligned:
                ri += 1
            aligned = not aligned
            continue
        ver = min(node_ver, net_vers.get(j, node_ver)) if nc_ver < 0x20 else node_ver
        if ver not in (0x20, 0x25):
            out[j] = (0, 0)
            ri += 0 if aligned else 1
            aligned = not aligned
            continue
        if aligned:
            g_n = (fmr[ri] >> 24) & 0xff
            g_v = (fmr[ri] >> 16) & 0xff
            o_n = fmr[ri] & 0xffff
            o_v = (fmr[ri + 1] >> 16) & 0xffff
            ri += 1
        else:
            g_n = (fmr[ri] >> 8) & 0xff
            g_v = fmr[ri] & 0xff
            o_n = (fmr[ri + 1] >> 16) & 0xffff
            o_v = fmr[ri + 1] & 0xffff
            ri += 2
        aligned = not aligned
        rate_n = LDPC_LEN * o_n // ((FFT_LEN + (g_n + 10) * 2) * 46) if g_n else 0
        rate_v = LDPC_LEN * o_v // ((FFT_LEN + (g_v + 10) * 2) * 46) if g_v else 0
        out[j] = (rate_n, rate_v)
    return out


class MocaAdapter:
    def __init__(self, host: str, username: str = "admin", password: str = "maxlinear", timeout: float = 8.0):
        self.base = f"http://{host}"
        self.timeout = timeout
        self._auth = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        self._lock = threading.Lock()
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))
        self._token = None

    def _open(self, req):
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            raise MocaError(f"HTTP {exc.code} z adaptera MoCA") from exc
        except OSError as exc:
            raise MocaUnreachable(f"Brak połączenia z adapterem MoCA: {exc}") from exc

    def _login(self) -> None:
        self._open(urllib.request.Request(self.base + "/devStatus.html", headers={"Authorization": self._auth}))
        self._token = next((c.value for c in self._jar if c.name == "csrf_token"), None)
        if not self._token:
            raise MocaError("Adapter MoCA nie podał tokenu CSRF")

    def query(self, path: str, data: list) -> list:
        with self._lock:
            for attempt in (1, 2):
                if not self._token:
                    self._login()
                req = urllib.request.Request(
                    self.base + path, data=json.dumps({"data": data}).encode(),
                    headers={"Authorization": self._auth, "X-CSRF-TOKEN": self._token,
                             "Content-Type": "application/x-www-form-urlencoded"})
                try:
                    body = self._open(req)
                    vals = json.loads(body)["data"]
                    return [int(v, 16) if isinstance(v, str) else int(v) for v in vals]
                except (MocaError, ValueError, KeyError) as exc:
                    if isinstance(exc, MocaUnreachable):
                        raise
                    self._token = None  # np. wygasły token - jedna ponowna próba z nowym
                    if attempt == 2:
                        raise MocaError(f"Zła odpowiedź adaptera MoCA ({path}): {exc}") from exc
        return []

    def read_status(self) -> dict:
        loc = parse_local(self.query("/ms/0/0x15", []))
        nodes = {}
        for i in range(MAX_NODES):
            if loc["node_mask"] >> i & 1:
                ni = self.query("/ms/0/0x16", [i])
                nodes[i] = {"mac": fmt_mac(ni[0], ni[1]), "ver": ni[4] & 0xff}
        m25 = self.query("/ms/0/0x7f", [])
        me = nodes.get(loc["node_id"], {})
        return {
            **loc,
            "mac": me.get("mac"),
            "nodes": nodes,
            "frames": parse_frames(self.query("/ms/0/0x14", [0])),
            "lof_mhz": m25[1] if len(m25) > 1 else None,
            "channels": m25[3] if len(m25) > 3 else None,
        }

    def read_phy(self, status: dict) -> dict:
        """{(mac_od, mac_do): Mb/s} - z odpowiedzi FMR każdego węzła (pomiar między adapterami)."""
        nodes, mask = status["nodes"], status["node_mask"]
        vers = {i: n["ver"] for i, n in nodes.items()}
        nc_ver = vers.get(status["nc_id"], 0x25)
        out = {}
        for i, n in nodes.items():
            final = 1 if min(nc_ver, n["ver"]) < 0x20 else 2
            fmr = self.query("/ms/0/0x1D", [1 << i, final])
            for j, (rate_n, rate_v) in parse_fmr(fmr, mask, n["ver"], nc_ver, vers).items():
                if j != i and j in nodes:
                    out[(n["mac"], nodes[j]["mac"])] = rate_n or rate_v
        return out
