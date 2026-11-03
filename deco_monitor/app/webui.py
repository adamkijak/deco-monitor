"""Panel WWW dodatku (HA ingress): incydenty, zdarzenia, archiwum logów, stan."""

from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("webui")

# Ingress Supervisora łączy się z tego adresu; lokalnie (testy) dopuszczamy localhost.
ALLOWED = {"172.30.32.2", "127.0.0.1", "::1"}

INDEX_HTML = r"""<!doctype html>
<html lang="pl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Deco Monitor</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c2330;--muted:#677386;--line:#e3e7ee;--ok:#1f9d55;--warn:#d98a00;
--bad:#d64545;--info:#3b7bd8;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--card:#1a1f26;--fg:#e6e9ef;--muted:#8d97a8;--line:#2a313b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--card);border-bottom:1px solid var(--line);padding:10px 16px}
h1{font-size:16px;margin:0 0 8px}.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;background:var(--bg);
border:1px solid var(--line);font-size:12.5px;white-space:nowrap}.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
.ok .dot{background:var(--ok)}.bad .dot{background:var(--bad)}.warn .dot{background:var(--warn)}
nav{display:flex;gap:4px;margin-top:10px;overflow-x:auto}nav button{border:0;background:none;color:var(--muted);
padding:6px 12px;border-radius:8px;font:inherit;cursor:pointer}nav button.on{background:var(--bg);color:var(--fg);font-weight:600}
main{padding:16px;max-width:1200px;margin:0 auto}.card{background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:12px 14px;margin-bottom:12px}.muted{color:var(--muted)}
.inc-h{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;cursor:pointer}.inc-h b{font-size:14.5px}
.tl{margin:10px 0 0;padding:0;list-style:none;border-left:2px solid var(--line)}.tl li{padding:2px 0 2px 12px;position:relative}
.tl li:before{content:"";position:absolute;left:-5px;top:9px;width:8px;height:8px;border-radius:50%;background:var(--muted)}
.tl li.problem:before{background:var(--bad)}.tl li.warning:before{background:var(--warn)}.tl li.info:before{background:var(--info)}
.t{font-family:var(--mono);color:var(--muted);margin-right:6px}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:600;color:var(--muted);font-size:12.5px}.sev{font-size:12px;padding:1px 7px;border-radius:6px}
.sev.problem{background:color-mix(in srgb,var(--bad) 18%,transparent);color:var(--bad)}
.sev.warning{background:color-mix(in srgb,var(--warn) 18%,transparent);color:var(--warn)}
.sev.info{background:color-mix(in srgb,var(--info) 15%,transparent);color:var(--info)}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px}
input,select{font:inherit;padding:5px 8px;border:1px solid var(--line);border-radius:7px;background:var(--card);color:var(--fg)}
input[type=search]{flex:1;min-width:180px}label{white-space:nowrap}
pre.log{margin:0;font:12px/1.5 var(--mono);white-space:pre-wrap;word-break:break-all;max-height:70vh;overflow:auto}
pre.log span.err{color:var(--bad)}pre.log span.warn{color:var(--warn)}pre.log span.hit{background:color-mix(in srgb,var(--info) 22%,transparent)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
a{color:var(--info)}.hidden{display:none}
</style></head><body>
<header><h1>Deco Monitor <span class="muted" id="ver"></span></h1><div class="chips" id="chips"></div>
<nav><button data-tab="inc" class="on">Incydenty</button><button data-tab="ev">Zdarzenia</button>
<button data-tab="log">Log Deco</button><button data-tab="state">Stan sieci</button></nav></header>
<main>
<section id="tab-inc"><div id="incs"></div></section>
<section id="tab-ev" class="hidden"><div class="bar"><select id="evsev"><option value="">wszystkie</option>
<option value="problem">problemy</option><option value="warning">ostrzeżenia</option><option value="info">informacje</option></select>
<input type="search" id="evq" placeholder="Szukaj w zdarzeniach…"></div><div class="card"><table><thead>
<tr><th>Czas</th><th>Waga</th><th>Zdarzenie</th></tr></thead><tbody id="evs"></tbody></table></div></section>
<section id="tab-log" class="hidden"><div class="bar"><select id="day"></select>
<input type="search" id="q" placeholder="Filtr (np. br-wan, smartip, bedroom)…">
<label><input type="checkbox" id="onlyerr"> tylko err/crit/warn</label>
<label><input type="checkbox" id="auto" checked> odświeżaj</label><a id="dl" href="#">pobierz plik</a></div>
<div class="muted" id="loginfo"></div><div class="card"><pre class="log" id="log"></pre></div></section>
<section id="tab-state" class="hidden"><div class="grid" id="state"></div></section>
</main>
<script>
const $=s=>document.querySelector(s), esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const get=async u=>{const r=await fetch(u);if(!r.ok)throw new Error(r.status);return r.json()};
let tab="inc";
document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>{tab=b.dataset.tab;
 document.querySelectorAll("nav button").forEach(x=>x.classList.toggle("on",x===b));
 document.querySelectorAll("main section").forEach(s=>s.classList.toggle("hidden",s.id!=="tab-"+tab));refresh()});
const chip=(cls,txt)=>`<span class="chip ${cls}"><span class="dot"></span>${esc(txt)}</span>`;
async function status(){const s=await get("api/status");$("#ver").textContent="v"+s.version;const v=s.values;let h="";
 h+=chip(v.internet?"ok":"bad","Internet");h+=chip(v.wan_link?"ok":"bad","Kabel WAN");
 h+=chip(v.incident_active?"bad":"ok",v.incident_active?"Trwa incydent":"Brak incydentu");
 for(const p of Object.values(s.pings))h+=chip(p.up===false?"bad":p.up?"ok":"",`${p.name} ${p.avg??"–"} ms`);
 for(const n of s.nodes)h+=chip(!n.online?"bad":(n.wired&&n.radio)?"warn":"ok",`${n.name}: ${n.wired&&n.radio?"na radiu!":n.conn}`);
 h+=chip(s.status.deco==="OK"?"ok":"bad","API Deco: "+s.status.deco);
 if(s.status.switch)h+=chip(s.status.switch==="OK"?"ok":"bad","Switch: "+s.status.switch);
 $("#chips").innerHTML=h;return s}
async function incs(){const list=await get("api/incidents?limit=100");
 $("#incs").innerHTML=list.length?list.map((i,k)=>`<div class="card"><div class="inc-h" onclick="this.nextElementSibling.classList.toggle('hidden')">
 <b>${esc(i.cause)}</b><span class="muted">${esc(i.start.replace("T"," "))} · ${i.duration_s} s</span></div>
 <ul class="tl ${k?"hidden":""}">${(i.effects||[]).length?`<li class="muted">Skutki: ${esc(i.effects.join("; "))}</li>`:""}
 ${i.timeline.map(t=>`<li class="${t.severity}"><span class="t">${t.time}</span>${esc(t.text)}</li>`).join("")}</ul></div>`).join("")
 :`<div class="card muted">Brak zarejestrowanych incydentów.</div>`}
async function evs(){const sev=$("#evsev").value,q=$("#evq").value.toLowerCase();let list=await get("api/events?limit=1500");
 list=list.filter(e=>(!sev||e.severity===sev)&&(!q||(e.message+e.type).toLowerCase().includes(q))).slice(0,500);
 $("#evs").innerHTML=list.map(e=>`<tr><td class="t">${esc(e.time.replace("T"," "))}</td><td><span class="sev ${e.severity}">${e.severity}</span></td>
 <td>${esc(e.message)}${e.historyczne?' <span class="muted">(z bufora przy starcie)</span>':""}</td></tr>`).join("")}
async function days(){const d=await get("api/days");const sel=$("#day"),cur=sel.value;
 sel.innerHTML=d.map(x=>`<option>${x}</option>`).join("");if(cur&&d.includes(cur))sel.value=cur}
async function logv(){if(!$("#day").options.length)await days();const day=$("#day").value;if(!day){$("#log").textContent="Brak archiwum.";return}
 const q=$("#q").value,lv=$("#onlyerr").checked?"err,crit,emerg,alert,warn":"";
 const r=await get(`api/log?day=${day}&q=${encodeURIComponent(q)}&levels=${lv}&limit=4000`);
 $("#dl").href=`api/download?day=${day}`;$("#loginfo").textContent=`${r.matched} z ${r.total} linii (pokazane ostatnie ${r.lines.length})`;
 const pre=$("#log"),atEnd=pre.scrollTop+pre.clientHeight>=pre.scrollHeight-20;
 const hit=/br-wan|conn-indicator|smartip|mobile_backup|ssh_connect|AP-STA-|Lease of/;
 pre.innerHTML=r.lines.map(l=>{const c=/\.(err|crit|emerg|alert) /.test(l)?"err":/\.warn /.test(l)?"warn":"";
  return `<span class="${c} ${hit.test(l)?"hit":""}">${esc(l)}</span>`}).join("\n");if(atEnd)pre.scrollTop=pre.scrollHeight}
async function state(){const s=await status();let h="";
 h+=`<div class="card"><b>Węzły Deco</b><table>${s.nodes.map(n=>`<tr><td>${esc(n.name)}<div class="muted">${esc(n.model)} · ${esc(n.ip)}</div></td>
 <td>${esc(n.conn)}${n.signal_dbm!=null?` · ${n.signal_dbm} dBm`:""}</td><td>${n.online?"online":"<b>offline</b>"}</td></tr>`).join("")}</table></div>`;
 h+=`<div class="card"><b>Ping (30 s)</b><table><tr><th>Cel</th><th>śr.</th><th>maks.</th><th>strata</th></tr>${Object.values(s.pings).map(p=>`<tr>
 <td>${esc(p.name)}<div class="muted">${esc(p.host)}</div></td><td>${p.avg??"–"} ms</td><td>${p.max??"–"} ms</td><td>${p.loss??"–"} %</td></tr>`).join("")}</table></div>`;
 if(s.ports.length)h+=`<div class="card"><b>Switch</b><table><tr><th>Port</th><th>Link</th><th>Rx błędy</th></tr>${s.ports.map(p=>`<tr>
 <td>${p.port} · ${esc(p.label)}</td><td>${esc(p.link)}</td><td>${p.rx_bad}</td></tr>`).join("")}</table></div>`;
 h+=`<div class="card"><b>Liczniki</b><table>${Object.entries(s.counters).map(([k,v])=>`<tr><td>${esc(k)}</td><td>${v}</td></tr>`).join("")}</table></div>`;
 $("#state").innerHTML=h}
async function refresh(){try{if(tab==="state")await state();else{await status();
 if(tab==="inc")await incs();if(tab==="ev")await evs();if(tab==="log")await logv()}}catch(e){console.error(e)}}
["#evsev","#evq"].forEach(s=>$(s).oninput=evs);["#day","#onlyerr"].forEach(s=>$(s).onchange=logv);
let t;$("#q").oninput=()=>{clearTimeout(t);t=setTimeout(logv,300)};
setInterval(()=>{if(tab!=="log"||$("#auto").checked)refresh()},10000);days().then(refresh);
</script></body></html>
"""


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
            path = u.path.rstrip("/").rsplit("/api/", 1)
            route = "api/" + path[1] if len(path) == 2 else ""
            try:
                if route == "api/status":
                    return self._json(mon.snapshot())
                if route == "api/days":
                    return self._json(store.days())
                if route == "api/log":
                    levels = {x for x in q.get("levels", "").split(",") if x} or None
                    return self._json(store.read_log(q.get("day", ""), q.get("q", ""), levels,
                                                     min(int(q.get("limit", 3000)), 20000)))
                if route == "api/events":
                    return self._json(store.events(min(int(q.get("limit", 500)), 5000)))
                if route == "api/incidents":
                    return self._json(store.incidents(min(int(q.get("limit", 100)), 2000)))
                if route == "api/download":
                    day = q.get("day", "")
                    r = store.read_log(day, limit=10**9)
                    body = ("\n".join(r["lines"]) + "\n").encode()
                    return self._send(200, body, "text/plain; charset=utf-8",
                                      {"Content-Disposition": f'attachment; filename="deco-{day}.log"'})
                if route == "":
                    return self._send(200, INDEX_HTML.encode(), "text/html; charset=utf-8")
                return self._send(404, b"not found", "text/plain")
            except (ValueError, OSError) as exc:
                return self._send(400, str(exc).encode(), "text/plain")

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True, name="webui").start()
    log.info("Panel WWW na porcie %d", port)
