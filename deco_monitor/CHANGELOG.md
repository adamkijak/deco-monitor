# Zmiany

## 1.1.0

- Panel dodatku z wykresami (bez zewnętrznych bibliotek, działa offline): oś czasu zaników 1 h – 7 dni, mapa
  mesh, przedni panel switcha, kafelki stanu, ostatni incydent z powiększoną osią czasu sekunda po sekundzie,
  wykresy ping / strata / CPU-RAM / klienci, problemy dzień po dniu. Tryb ciemny i widok na telefon.
- Historia pomiarów co 30 s w `/share/deco_monitor/metrics/` (do wykresów).
- Pulpit „Sieć Deco” tworzony automatycznie w HA – z kartami Mushroom/ApexCharts, gdy są w HACS, inaczej
  z wbudowanymi; ręcznych zmian nie nadpisuje. Nowa opcja `auto_dashboard`.
- Log Deco: przełącznik „ukryj szum modemu LTE” (`[mobile]`, `simplecom` – ok. 70% linii).
- Switch: ponowne próby przy zerwanym połączeniu, błąd dopiero po 3 nieudanych odczytach z rzędu.
- Incydenty: w skutkach „na radiu” tylko satelity, które zgubiły kabel.

## 1.0.0

- Pierwsza wersja: archiwum syslogu Deco, rozpoznawanie zdarzeń (zanik kabla WAN, internet offline,
  wyrzucenie klientów WiFi, reset portów, restart DHCP, łącze zapasowe LTE, satelita na radiu zamiast kabla,
  niedostępne satelity, masowe rozłączenia WiFi), stan węzłów mesh z API Deco, porty switcha TP-Link Easy Smart,
  ping co 2 s z wykrywaniem zaników, incydenty z podsumowaniem, encje MQTT, generowany dashboard i panel
  z przeglądarką logów.
