"""Generuje YAML dashboardu Lovelace z encji, które dodatek faktycznie utworzył.

Wymaga kart z HACS: Mushroom (custom:mushroom-*) i ApexCharts Card.
"""

from __future__ import annotations

import yaml

def _tpl_card(primary: str, secondary: str, icon: str, color: str, entity: str | None = None) -> dict:
    card = {
        "type": "custom:mushroom-template-card",
        "primary": primary,
        "secondary": secondary,
        "icon": icon,
        "icon_color": color,
        "multiline_secondary": True,
    }
    if entity:
        card["entity"] = entity
        card["tap_action"] = {"action": "more-info"}
    return card


def _apex(title: str, series: list, span: str = "24h", group: str = "5min", func: str = "avg",
          unit: str | None = None, chart_type: str = "line") -> dict:
    card = {
        "type": "custom:apexcharts-card",
        "graph_span": span,
        "header": {"show": True, "title": title, "show_states": True, "colorize_states": True},
        "apex_config": {"chart": {"height": 260}, "legend": {"show": True}},
        "series": [],
    }
    for s in series:
        item = {"entity": s["entity"], "name": s["name"], "type": chart_type, "stroke_width": 2,
                "group_by": {"func": func, "duration": group}}
        if unit:
            item["unit"] = unit
        card["series"].append(item)
    return card


LONG_TEXT = ("last_incident", "last_event")


def to_builtin(card: dict, long_text: set) -> dict:
    """Zamienia karty z HACS na wbudowane odpowiedniki (gdy Mushroom/ApexCharts nie są zainstalowane)."""
    t = card.get("type", "")
    if "cards" in card:
        return {**card, "cards": [to_builtin(c, long_text) for c in card["cards"]]}
    if t == "custom:mushroom-chips-card":
        tiles = [{"type": "tile", "entity": c["entity"], **({"name": c["content"]} if "{{" not in str(c.get("content", ""))
                                                              and c.get("content") else {})}
                 for c in card["chips"] if c.get("entity")]
        return {"type": "grid", "columns": 4, "square": False, "cards": tiles}
    if t == "custom:mushroom-template-card":
        if card.get("entity") and card["entity"] not in long_text:
            return {"type": "tile", "entity": card["entity"], "name": card["primary"]}
        return {"type": "markdown", "content": f"**{card['primary']}**\n\n{card['secondary']}"}
    if t == "custom:apexcharts-card":
        return {"type": "history-graph", "title": card["header"]["title"], "hours_to_show": 24,
                "entities": [{"entity": s["entity"], "name": s["name"]} for s in card["series"]]}
    return card


def build_dashboard(mon) -> str:
    """YAML do ręcznego wklejenia (wariant z kartami z HACS)."""
    header = ("# Dashboard wygenerowany przez dodatek Deco Monitor.\n"
              "# Wymaga kart z HACS: Mushroom, ApexCharts Card (wersja bez nich: dashboard-wbudowane.yaml).\n"
              "# Ustawienia > Pulpity > Dodaj pulpit > Nowy od zera > ⋮ > Edytor kodu (raw) > wklej.\n")
    return header + yaml.safe_dump(build_config(mon, True), allow_unicode=True, sort_keys=False, width=120)


def build_config(mon, hacs: bool) -> dict:
    cfg = _build(mon)
    if hacs:
        return cfg
    long_text = {mon.ents[k] for k in LONG_TEXT if k in mon.ents}
    return {**cfg, "views": [{**v, "cards": [to_builtin(c, long_text) for c in v["cards"]]} for v in cfg["views"]]}


def _build(mon) -> dict:
    e = mon.ents
    has = e.__contains__

    def st(key: str) -> str:
        return e[key]

    chips = []
    for key, label, icon in (("internet", "Internet", "mdi:web"), ("wan_link", "Kabel WAN", "mdi:ethernet-cable")):
        chips.append({
            "type": "template", "entity": st(key), "icon": icon, "content": label,
            "icon_color": f"{{{{ 'green' if is_state('{st(key)}', 'on') else 'red' }}}}",
            "tap_action": {"action": "more-info"},
        })
    chips.append({
        "type": "template", "entity": st("incident_active"), "icon": "mdi:alert-octagon",
        "content": f"{{{{ 'Trwa incydent' if is_state('{st('incident_active')}', 'on') else 'Brak incydentu' }}}}",
        "icon_color": f"{{{{ 'red' if is_state('{st('incident_active')}', 'on') else 'grey' }}}}",
    })
    for key in ("wan_ip", "cpu", "mem", "clients_wifi", "clients_wired"):
        chips.append({"type": "entity", "entity": st(key), "content_info": "state"})

    inc = st("last_incident")
    incident_card = _tpl_card(
        "Ostatni incydent",
        f"{{{{ states('{inc}') }}}}",
        "mdi:alert-octagon",
        f"{{{{ 'red' if is_state('{st('incident_active')}', 'on') else 'orange' }}}}",
        inc,
    )
    event_card = _tpl_card("Ostatnie zdarzenie", f"{{{{ states('{st('last_event')}') }}}}", "mdi:bell-ring",
                           "blue", st("last_event"))

    node_cards = []
    for name, info in sorted(mon.nodes.items(), key=lambda kv: (not kv[1]["master"], kv[0])):
        conn, online = st(f"node_{name}_conn"), st(f"node_{name}_online")
        radio = e.get(f"node_{name}_radio")
        sig = e.get(f"node_{name}_signal")
        secondary = f"{{{{ states('{conn}') }}}}"
        parent = e.get(f"node_{name}_parent")
        if parent:
            secondary = f"→ {{{{ states('{parent}') }}}} · " + secondary
        if sig:
            secondary += f" · {{{{ states('{sig}') }}}} dBm"
        speed = e.get(f"node_{name}_bh_speed")
        if speed:
            secondary += f" · {{{{ states('{speed}') }}}} Mbit/s"
        if radio and info["wired"]:
            color = (f"{{% if is_state('{online}', 'off') %}}red{{% elif is_state('{radio}', 'on') %}}"
                     f"orange{{% else %}}green{{% endif %}}")
            secondary += f"{{% if is_state('{radio}', 'on') %}} · ⚠ na radiu zamiast kabla{{% endif %}}"
        else:
            color = f"{{{{ 'green' if is_state('{online}', 'on') else 'red' }}}}"
        icon = "mdi:router-network" if info["master"] else "mdi:router-wireless"
        node_cards.append(_tpl_card(name, secondary, icon, color, conn))

    ping_cards, ping_rtts, ping_loss = [], [], []
    ping_bins_wan, ping_bins_mesh = [], []
    for key, meta in mon.ping_meta.items():
        b, rtt, loss = st(f"ping_{key}"), st(f"ping_{key}_rtt"), st(f"ping_{key}_loss")
        ping_cards.append(_tpl_card(
            meta["name"],
            f"{{{{ states('{rtt}') }}}} ms · strata {{{{ states('{loss}') }}}} %",
            {"internet": "mdi:web", "local": "mdi:lan", "mesh": "mdi:access-point"}.get(meta["kind"], "mdi:lan"),
            f"{{{{ 'green' if is_state('{b}', 'on') else 'red' }}}}",
            b,
        ))
        (ping_bins_mesh if meta["kind"] == "mesh" else ping_bins_wan).append({"entity": b, "name": meta["name"]})
        ping_rtts.append({"entity": rtt, "name": meta["name"]})
        ping_loss.append({"entity": loss, "name": meta["name"]})

    timeline_wan = [
        {"entity": st("internet"), "name": "Internet"},
        {"entity": st("wan_link"), "name": "Kabel WAN"},
        {"entity": st("incident_active"), "name": "Incydent"},
    ] + ping_bins_wan
    timeline_lan = []
    for name, info in sorted(mon.nodes.items()):
        if e.get(f"node_{name}_radio") and info["wired"]:
            timeline_lan.append({"entity": e[f"node_{name}_radio"], "name": f"{name}: na radiu"})
        timeline_lan.append({"entity": st(f"node_{name}_online"), "name": f"{name} online"})
    timeline_lan += ping_bins_mesh
    bh_speeds = [{"entity": e[f"node_{name}_bh_speed"], "name": name}
                 for name in sorted(mon.nodes) if e.get(f"node_{name}_bh_speed")]
    port_links = [{"entity": st(f"port{n}_link"), "name": f"Port {n} ({mon.port_labels.get(n)})"}
                  for n in range(1, 9) if has(f"port{n}_link")]

    overview = {
        "title": "Przegląd",
        "path": "przeglad",
        "icon": "mdi:router-network",
        "cards": [
            {"type": "vertical-stack", "cards": [
                {"type": "markdown", "content":
                    f"📊 **Oś czasu zaników, mapa mesh, switch i archiwum logów:** "
                    f"[otwórz panel Deco Monitor]({getattr(mon, 'ingress_url', '/hassio/ingress/local_deco_monitor')})"},
                {"type": "custom:mushroom-chips-card", "chips": chips},
                incident_card,
                event_card,
            ]},
            {"type": "grid", "columns": 2, "square": False, "title": "Węzły Deco", "cards": node_cards},
            {"type": "markdown", "title": "Topologia mesh (kto do kogo jest podłączony)",
             "content": f"{{{{ state_attr('{st('mesh_topology')}', 'drzewo') or 'brak danych' }}}}"},
            {"type": "grid", "columns": 2, "square": False, "title": "Ping", "cards": ping_cards},
            {"type": "history-graph", "title": "Oś czasu: internet i WAN (24 h)", "hours_to_show": 24,
             "entities": timeline_wan},
            {"type": "history-graph", "title": "Oś czasu: mesh i sieć lokalna (24 h)", "hours_to_show": 24,
             "entities": timeline_lan},
        ] + ([
            {"type": "history-graph", "title": "Prędkość kabla do satelit (48 h)", "hours_to_show": 48,
             "entities": bh_speeds},
        ] if bh_speeds else []) + [
            {"type": "logbook", "title": "Incydenty i zdarzenia", "hours_to_show": 48,
             "entities": [st("last_incident"), st("last_event")]},
        ],
    }

    counters_main = [f"cnt_{k}" for k in ("incidents", "wan_link_drops", "internet_drops", "ping_outages",
                                         "backhaul_fallbacks", "clients_kicked", "lan_dhcp_restarts",
                                         "wifi_mass_disconnects", "switch_link_drops")]
    charts = {
        "title": "Wykresy",
        "path": "wykresy",
        "icon": "mdi:chart-line",
        "cards": [
            _apex("Opóźnienie ping (średnia 5 min)", ping_rtts, unit="ms"),
            _apex("Strata pakietów (maks. w 5 min)", ping_loss, func="max", unit="%", chart_type="column"),
            _apex("Obciążenie głównego Deco", [{"entity": st("cpu"), "name": "CPU"},
                                               {"entity": st("mem"), "name": "RAM"}], unit="%"),
            _apex("Klienci", [{"entity": st("clients_wifi"), "name": "WiFi"},
                              {"entity": st("clients_wired"), "name": "Kabel"}], func="last"),
            {"type": "statistics-graph", "title": "Problemy dziennie (14 dni)", "days_to_show": 14,
             "period": "day", "chart_type": "bar", "stat_types": ["change"],
             "entities": [st(k) for k in counters_main]},
            {"type": "statistics-graph", "title": "Połączenia / rozłączenia WiFi na godzinę", "days_to_show": 2,
             "period": "hour", "chart_type": "bar", "stat_types": ["change"],
             "entities": [st("cnt_wifi_connects"), st("cnt_wifi_disconnects")]},
        ],
    }

    views = [overview, charts]

    if mon.switch:
        port_cards = []
        for n in range(1, 9):
            link, speed, rate = st(f"port{n}_link"), st(f"port{n}"), st(f"port{n}_rate")
            port_cards.append(_tpl_card(
                f"Port {n} · {mon.port_labels.get(n)}",
                f"{{{{ states('{speed}') }}}} · {{{{ states('{rate}') }}}} pkt/s · błędy RX "
                f"{{{{ states('{st(f'port{n}_rxbad')}') }}}}",
                "mdi:ethernet",
                f"{{{{ 'green' if is_state('{link}', 'on') else 'grey' }}}}",
                speed,
            ))
        active = [n for n, up in mon.port_up.items() if up] or list(range(1, 9))
        views.append({
            "title": "Switch",
            "path": "switch",
            "icon": "mdi:switch",
            "cards": [
                {"type": "grid", "columns": 2, "square": False, "title": "TL-SG108E", "cards": port_cards},
                {"type": "history-graph", "title": "Link portów (24 h)", "hours_to_show": 24,
                 "entities": port_links},
                _apex("Ruch na portach (pkt/s)", [{"entity": st(f"port{n}_rate"), "name": f"Port {n}"}
                                                  for n in active]),
                {"type": "statistics-graph", "title": "Błędy RX na godzinę", "days_to_show": 2, "period": "hour",
                 "chart_type": "bar", "stat_types": ["change"],
                 "entities": [st(f"port{n}_rxbad") for n in active]},
            ],
        })

    log_ent = st("log_line")
    views.append({
        "title": "Log",
        "path": "log",
        "icon": "mdi:text-box-outline",
        "type": "panel",
        "cards": [{"type": "vertical-stack", "cards": [
            {"type": "markdown", "content":
                f"**Pełny log, archiwum i oś incydentów:** [otwórz panel Deco Monitor]({getattr(mon, 'ingress_url', '/hassio/ingress/local_deco_monitor')})"},
            {"type": "markdown", "title": "Syslog Deco – ostatnie linie", "content":
                "<pre>{% for l in state_attr('" + log_ent + "', 'linie') or [] %}{{ l }}\n{% endfor %}</pre>"},
        ]}],
    })

    return {"title": "Sieć Deco", "views": views}
