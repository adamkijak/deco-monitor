# deco-monitor

Monitoring sieci TP-Link Deco w Home Assistant: archiwum syslogu Deco (router trzyma tylko ~10 minut),
rozpoznane zdarzenia (zanik kabla WAN, utrata kabla przez satelitę, wyrzucanie klientów WiFi przez Deco,
restart DHCP), stan mesh, porty switcha TP-Link Easy Smart i ping co 2 s. Wszystko składa się w **incydenty**
z jednym zdaniem podsumowania, np.

> 17:09:34 Zanik kabla WAN, 31 s; restart DHCP LAN ×1; na radiu: bedroom; brak pingu: Google DNS, Cloudflare

Encje trafiają do HA przez MQTT discovery, a dodatek generuje gotowy dashboard (Mushroom + ApexCharts)
i ma własny panel w pasku bocznym z przeglądarką logów.

## Home Assistant OS

To repozytorium jest repozytorium dodatków HA (`repository.yaml`, dodatek w `deco_monitor/`).

Ustawienia → Dodatki → Sklep z dodatkami → ⋮ → Repozytoria → dodaj `https://github.com/adamkijak/deco-monitor`,
potem **Deco Monitor** → Instaluj → hasła w zakładce Konfiguracja → Uruchom.

Szczegóły: [deco_monitor/DOCS.md](deco_monitor/DOCS.md).

## Na komputerze

Test bez Home Assistant (bez MQTT – publikacje tylko w logu; panel na http://localhost:8099/):

```bash
DECO_PASSWORD=... SWITCH_PASSWORD=... scripts/run_local.sh
python3 -m unittest discover tests
```

Potrzebne: Python 3.8+, `cryptography`, `PyYAML` (i `paho-mqtt` tylko z prawdziwym brokerem).

## Układ

```
deco_monitor/            add-on (kontekst budowania obrazu)
  app/main.py            pętle, encje, incydenty
  app/deco_api.py        lokalne API Deco (RSA + AES), eksport syslogu
  app/syslog_parser.py   reguły rozpoznawania zdarzeń, zszywanie kolejnych zrzutów bufora
  app/switch_api.py      odczyt portów TL-SG108E
  app/pinger.py          ping z wykrywaniem zaników
  app/incidents.py       składanie sygnałów w incydenty
  app/mqtt_pub.py        MQTT discovery
  app/dashboard.py       generator dashboardu Lovelace
  app/webui.py           panel (Ingress)
  config.yaml build.yaml Dockerfile run.sh
tests/                   python3 -m unittest discover tests
scripts/run_local.sh
```
