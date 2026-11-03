# Deco Monitor

Monitoring domowej sieci TP-Link Deco w Home Assistant. Dodatek zbiera dane z czterech źródeł
i składa je w jeden obraz tego, co się dzieje w sieci:

| źródło | co daje | jak często |
|---|---|---|
| **syslog głównego Deco** (to samo co „Eksportuj log” w panelu WWW) | archiwum logów na dysku + rozpoznane zdarzenia | co 10 s |
| **lokalne API Deco** | węzły mesh: online, kabel/radio, sygnał; WAN, CPU/RAM, liczba klientów | co 30 s |
| **switch TP-Link Easy Smart** (TL-SG108E, TL-SG105E…) | link i prędkość portów, ruch, błędy RX, wykrywanie restartu | co 10 s |
| **ping** | internet, główne Deco, switch i każda satelita: dostępność, opóźnienie, strata | co 2 s |

Router trzyma w pamięci tylko ~10 minut logu – dodatek zapisuje go na stałe
(`/share/deco_monitor/logs/deco-RRRR-MM-DD.log`, domyślnie 30 dni).

## Rozpoznawane zdarzenia

| zdarzenie | wpis w logu Deco |
|---|---|
| Kabel WAN: brak linku / link przywrócony | `netifd: Network device 'br-wan' link is down/up` |
| Internet offline / online | `conn-indicator: Connectivity changed` |
| Deco wyrzuciło wszystkich klientów WiFi | `smartip: [lan] disconnect wifi(lan) wifi client!!` (przy zmianie DNS) |
| Reset portów Ethernet | `smartip: [lan] ##### disconnect eth port N` |
| Restart serwera DHCP sieci lokalnej | `smartip: [lan] kill process udhcpd` |
| Przełączenie WAN → LTE i z powrotem | `mobile_backup: ewan_disconnect / ewan_recovered` |
| Satelita na kablu przeszła na radio (i wróciła) | `hostapd: AP-STA-CONNECTED` z adresem satelity |
| Główne Deco nie może połączyć się z satelitą | `ssh_connect: Timeout connecting to …` |
| Masowe rozłączenia WiFi (≥ 6 w minutę), restart Deco | `AP-STA-DISCONNECTED`, start syslogd |

Do tego zdarzenia z innych źródeł: brak pingu i powrót (z czasem zaniku), utrata linku na porcie switcha,
restart switcha (wyzerowane liczniki), węzeł offline, zmiana połączenia węzła.

### Incydenty

Każdy problem otwiera **incydent**, a kolejne sygnały do niego dołączają. Incydent zamyka się, gdy wszystkie
pingi odpowiadają i przez ~35 s nie przyszło nic nowego. Wtedy powstaje jedno podsumowanie, np.:

> 17:09:34 Zanik kabla WAN, 31 s; restart DHCP LAN ×1; na radiu: bedroom; brak pingu: Google DNS, Cloudflare

Podsumowanie trafia do `sensor.deco_monitor_ostatni_incydent` (pełna oś czasu w atrybutach), do encji
zdarzeń (`event_type: incident`) i do panelu dodatku.

Zdarzenia z logu docierają do HA z opóźnieniem ~15 s (co tyle pobierany jest log), ale **z zachowaniem
odstępów czasowych** – 8-sekundowy zanik kabla jest na osi czasu widoczny jako 8 s. Dokładny czas z routera
jest w atrybucie `czas`.

## Wymagania

1. **Broker MQTT** – dodatek *Mosquitto broker* + integracja MQTT (dodatek sam pobiera dane logowania).
2. Dla dashboardu: z **HACS** karty **Mushroom** i **ApexCharts Card**.

## Instalacja

Ustawienia → Dodatki → Sklep z dodatkami → ⋮ → Repozytoria → dodaj
`https://github.com/adamkijak/deco-monitor` → **Deco Monitor** → Instaluj.

W zakładce **Konfiguracja** wpisz co najmniej `deco_password` (i `switch_password`, jeśli masz switch),
potem Uruchom. Jeśli używasz starego dodatku *Deco Syslog Bridge* – odinstaluj go (robi to samo, gorzej).

## Opcje

| opcja | znaczenie |
|---|---|
| `deco_host`, `deco_password` | IP głównego Deco i hasło administratora (to samo co w aplikacji Deco). |
| `syslog_interval` | Co ile sekund pobierać log (każde pobranie to przebudowa eksportu na routerze – nie schodź poniżej 5). |
| `api_interval` | Co ile sekund odpytywać API Deco o węzły, WAN, CPU/RAM i klientów. |
| `switch_host`, `switch_username`, `switch_password` | Switch Easy Smart. Puste hasło = bez switcha. Tylko odczyt. |
| `switch_interval` | Co ile sekund czytać porty (serwer WWW switcha jest słaby – 10 s to rozsądne minimum). |
| `switch_port_names` | Opisy portów 1–8 (np. `Deco Poddasze`, `bedroom`, `TV`) – w zdarzeniach i na dashboardzie. |
| `ping_interval` | Co ile sekund pingować (zanik = 2 kolejne pingi bez odpowiedzi). |
| `ping_targets` | Cele pingu: `name`, `host`, `kind` (`internet` / `local` / `mesh`). |
| `ping_mesh_nodes` | Automatycznie pinguj wszystkie satelity Deco (adresy z API). |
| `log_retention_days` | Ile dni trzymać archiwum logów. |
| `mqtt_*` | Tylko gdy broker MQTT nie jest dodatkiem HA. |

## Encje

Wszystkie należą do urządzenia **Deco Monitor** i mają prefiks `deco_monitor_`, m.in.:

- `binary_sensor.deco_monitor_internet`, `…_kabel_wan`, `…_trwa_incydent`
- `sensor.deco_monitor_ostatni_incydent`, `…_ostatnie_zdarzenie`, `event.deco_monitor_zdarzenia`
- dla każdego węzła: `sensor.deco_monitor_<węzeł>_polaczenie` (kabel / radio 5 GHz…), `binary_sensor…_<węzeł>_online`,
  `…_<węzeł>_backhaul_radiowy`, `sensor…_<węzeł>_sygnal`
- dla każdego celu pingu: `binary_sensor.deco_monitor_ping_<cel>`, `sensor…_ping_<cel>_opoznienie`, `…_strata`
- dla portów switcha: `sensor.deco_monitor_switch_port_N`, `…_link`, `…_bledy_rx`, `…_ruch`
- liczniki (rosnące, do statystyk dziennych): zaniki kabla WAN, zaniki internetu, wyrzucenia klientów, restarty
  DHCP, utraty kabla satelit, rozłączenia WiFi, incydenty, zaniki ping, utraty linku na switchu…
- `sensor.deco_monitor_cpu_deco`, `…_ram_deco`, `…_adres_ip_wan`, `…_klienci_wifi`, `…_klienci_przewodowi`

**Recorder:** `sensor.deco_monitor_log_ostatnie_linie` ma w atrybutach 40 ostatnich linii logu i zmienia się co
10 s. Żeby nie puchła baza, wyklucz go w `configuration.yaml`:

```yaml
recorder:
  exclude:
    entities:
      - sensor.deco_monitor_log_ostatnie_linie
```

## Dashboard

Po starcie dodatek zapisuje gotowy dashboard z prawdziwymi nazwami encji do
`/share/deco_monitor/dashboard.yaml` (odświeża go, gdy dojdzie węzeł albo cel pingu).

Ustawienia → Pulpity → Dodaj pulpit → Nowy pulpit od zera → otwórz go → ✏️ → ⋮ → **Edytor kodu** → wklej
zawartość pliku. Plik otworzysz np. dodatkiem *File editor* / *Studio Code Server* albo przez Sambę (`share`).

Widoki: **Przegląd** (stan, ostatni incydent, węzły, pingi, oś czasu 24 h, dziennik), **Wykresy** (opóźnienia,
straty, CPU/RAM, problemy dziennie), **Switch** (porty, link, ruch, błędy) i **Log** (ostatnie linie + link do panelu).

## Panel dodatku

**Deco Monitor** w pasku bocznym: lista incydentów z osią czasu, wszystkie zdarzenia z filtrem, przeglądarka
archiwum logów (wybór dnia, wyszukiwanie, tylko błędy, pobieranie pliku) i bieżący stan sieci.

## Powiadomienia

Przykład automatyzacji – powiadomienie po każdym incydencie:

```yaml
alias: Sieć – incydent
triggers:
  - trigger: state
    entity_id: event.deco_monitor_zdarzenia
conditions:
  - condition: template
    value_template: "{{ trigger.to_state.attributes.event_type == 'incident' }}"
actions:
  - action: notify.notify
    data:
      title: Sieć Deco
      message: "{{ trigger.to_state.attributes.message }}"
```

## Uwagi

- Integracja [tplink_deco](https://github.com/amosyuen/ha-tplink-deco) i ten dodatek logują się do Deco jako ten
  sam administrator. Deco czasem unieważnia wtedy starszą sesję – oba same logują się ponownie. Gdy w logu dodatku
  często widać „Błąd API”, zwiększ `api_interval` albo interwał odświeżania integracji.
- Reguły rozpoznawania zdarzeń powstały na Deco X50-4G (fw 1.2.0) i X50 (fw 1.8.0). Inne modele mogą pisać
  w logu inaczej – archiwum i tak zapisuje wszystko.
