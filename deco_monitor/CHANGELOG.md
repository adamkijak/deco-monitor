# Zmiany

## 1.3.5

- **Węzeł usunięty z sieci Deco znika sam.** Gdy węzła nie ma na liście Deco przez 10 odczytów z rzędu (ok. 5 min),
  dodatek przestaje go pingować i pobierać jego log, a jego encje znikają z Home Assistant. Węzeł offline nadal
  jest na tej liście, więc to dotyczy tylko węzłów usuniętych w aplikacji Deco.
- Oś czasu nie pokazuje pasów satelit, których nie ma już w sieci, także z ich wcześniejszą historią.
- **Poprawka: pas „Ping Satelita …” po usunięciu węzła.** Przerwa w pingu, która nie zdążyła się zamknąć,
  trafiała na osobny pas zwykłego pingu, bo rodzaj celu był brany tylko z bieżącej listy celów.
  Teraz jest brany też z samego zdarzenia.
- **Poprawka: zduplikowane konfiguracje MQTT.** Po restarcie Home Assistant dodatek publikował konfigurację encji
  ponownie, ale pod złym tematem (`homeassistant/<domena>/deco_monitor/<domena>.deco_monitor_x/config`).
  Te kopie zostawały w brokerze i po kolejnym restarcie HA odtwarzały encje, które już usunięto.
  Ponowna publikacja używa teraz właściwego tematu, a dodatek przy połączeniu z MQTT usuwa stare kopie.

## 1.3.4

- **Poprawka: satelity pod samym adresem IP.** „Główne Deco nie może połączyć się z satelitą …” pochodzi z logu,
  w którym jest tylko IP. Nazwa była brana z bieżącej listy węzłów, a w niej satelita offline nie ma adresu IP,
  więc właśnie niedostępna satelita zostawała pod samym IP. Na osi czasu powstawały przez to dodatkowe pasy
  „Satelita 192.168.71.x”, a w incydentach ta sama satelita pojawiała się dwa razy (pod IP i pod nazwą).
  Dodatek pamięta teraz IP każdego węzła, także po restarcie (`node_ips.json`).
- Oś czasu przypisuje też starsze zdarzenia z samym IP do pasa właściwej satelity.

## 1.3.3

- **Poprawka: fałszywy „Brak internetu” w incydentach.** Incydent dostawał przyczynę „Brak internetu (kabel WAN OK)”,
  gdy w trakcie choć raz nie odpowiedział jeden z celów pingu w internecie. Tak 5 s bez odpowiedzi z Cloudflare
  przemianowało 2-godzinną pętlę rozłączeń satelity na brak internetu. Teraz ta przyczyna wymaga, żeby wszystkie
  cele w internecie milczały jednocześnie albo żeby samo Deco zgłosiło brak internetu.
- Gdy nie odpowiada tylko część celów, a nic innego się nie działo, przyczyna brzmi
  „Brak pingu do Cloudflare (internet działał)”.

## 1.3.2

- **Kto do kogo jest podłączony.** Dla każdej satelity nowy czujnik `sensor.<satelita>_podlaczony_do` z nazwą
  węzła nadrzędnego, przez który satelita łączy się z siecią (np. kuchnia → salon albo kuchnia → Poddasze).
  X50 podaje go wprost (`parent_device_id`); M3W nie podaje, więc dodatek bierze MAC rodzica z pola `previous`.
- Zmiana rodzica to zdarzenie „Węzeł X: podłączony do Y (było: Z)” (ostrzeżenie, typ `node_parent_change`).
  Zdarzenie trafia do dziennika i jako punkt na pas satelity na osi czasu.
- Czujnik `sensor.deco_monitor_topologia_mesh`: krótki opis drzewa w stanie, pełne drzewo w markdownie
  w atrybucie `drzewo`.
- Pulpit „Sieć Deco”: kafelki węzłów zaczynają się od „→ rodzic”, a przegląd ma nową kartę „Topologia mesh”.
- Panel: mapa mesh rysuje prawdziwe drzewo połączeń (satelita pod swoim rodzicem) zamiast gwiazdy wokół głównego Deco.

## 1.3.1

- **Prędkość kabla do satelit.** Nowy czujnik `sensor.<satelita>_predkosc_kabla` (Mbit/s) dla każdej satelity
  podłączonej kablem. Pokazuje prędkość połączenia kablowego, którą zgłasza Deco (`backhual_speed`
  w liście węzłów, np. 1000/100/10). Gdy satelita przechodzi na radio, czujnik pokazuje 0.
- Zmiana prędkości to zdarzenie „Węzeł X: prędkość kabla 100 → 10 Mbit/s” (spadek jako ostrzeżenie, wzrost jako
  informacja). Zdarzenie pojawia się w dzienniku i w panelu, a encja zdarzeń ma nowy typ `node_backhaul_speed`.
- Pulpit „Sieć Deco”: kafelki węzłów pokazują prędkość kabla, a przegląd ma nowy wykres „Prędkość kabla do satelit (48 h)”.

## 1.3.0

- **Logi satelit.** Log głównego Deco zawiera tylko jego własne radia, więc to, co dzieje się po drugiej stronie
  radiowego połączenia satelity, było niewidoczne. Dodatek co 5 minut pobiera teraz eksport logu z każdej satelity,
  która go udostępnia, i zapisuje go osobno: `logs/deco-<satelita>-RRRR-MM-DD.log` (z tą samą deduplikacją
  co log głównego Deco). Działa z X50 (HTTPS) i M4R (HTTP); M3W 1.0.x odrzuca logowanie.
- Starsze satelity (M4R) mają panel tylko po HTTP albo z TLS 1.0 – dodatek obsługuje oba warianty. Satelity, które
  odrzucają logowanie (M3W 1.0.x), są sprawdzane ponownie co 6 h; ich stan widać w statusie monitora.
- Panel: w zakładce **Log Deco** wybór źródła (główne Deco / satelita), filtr i „pobierz plik” działają dla każdego.
- Nowe opcje: `satellite_syslog` (domyślnie włączone), `satellite_syslog_interval` (domyślnie 300 s)
  i `satellite_log_retention_days` (domyślnie 7 dni – log X50 to ~40–70 MB na dobę).

## 1.2.3

- Ping do switcha nie jest mierzony w trakcie odczytu jego panelu WWW i 2 s po nim. Na ping odpowiada ten sam słaby
  procesor, który generuje stronę ze statystykami – pomiar pokazywał 30–200 ms, które dodatek sam wywoływał
  (ruch przez switch idzie układem sprzętowym i nie zwalnia: urządzenia za switchem 4–7 ms).

## 1.2.2

- Log Deco: filtr „tylko err/crit” zastąpiony filtrem **„tylko istotne”**. Deco zapisuje rutynę (DHCP, menedżer
  klientów, serwer WWW, sterowanie roamingiem) jako `daemon.err`, więc dawny filtr pokazywał prawie wszystko.
  Teraz zostają wpisy rozpoznawane jako zdarzenia (WAN, internet, smartip, LTE, satelity, WiFi, restart DHCP),
  urządzenie znikające z sieci (`disconnect_counter` ≥ 3) oraz błędy spoza znanej rutyny. Ten sam filtr jest
  w logach przy incydentach i zdarzeniach.

## 1.2.1

- Przy pierwszym starcie usuwane są fałszywe alarmy „masowe rozłączenia WiFi” zapisane przez wersje < 1.2.0
  (sprawdzane na oczyszczonym archiwum: w logu z tej minuty mniej niż 5 różnych urządzeń) oraz incydenty, które
  tylko z nich powstały. „Ostatni incydent” i „Ostatnie zdarzenie” pokazują potem ostatni prawdziwy wpis.

## 1.2.0

- **Naprawa fałszywych alarmów:** eksport logu z Deco co kilka pobrań jest nieaktualny albo ucięty (router oddaje
  sam początek bufora). Wersje 1.0–1.1 dopisywały wtedy stare linie drugi raz i analizowały je ponownie – stąd np.
  „Masowe rozłączenia WiFi: 6 urządzeń”, gdy rozłączyło się jedno urządzenie kilka razy policzone. Teraz każda linia
  jest porównywana z tym, co już jest w archiwum (z krotnością), a pamięć odbudowuje się z archiwum po restarcie.
  Przy pierwszym starcie 1.2.0 archiwum jest jednorazowo czyszczone z duplikatów (kopia: `*.przed-1.2.0`).
- Rozłączenia WiFi ze szczegółami: nazwa urządzenia (z aplikacji Deco), MAC, pasmo, czy i po ilu sekundach wróciło.
  Masowe rozłączenie liczy **różne** urządzenia (≥ 5 w 60 s). Nowe zdarzenie: urządzenie rozłącza się ≥ 4× w 10 min.
- Incydenty: tabela rozłączonych urządzeń i **log Deco z okresu incydentu** (panel; 40 linii także w atrybucie
  `log` encji `sensor.deco_monitor_ostatni_incydent`). Czas incydentu WiFi: od pierwszego rozłączenia do ostatniego
  powrotu.
- Zdarzenia w panelu rozwijają się: szczegóły (urządzenia, węzeł, port…) i log ±30 s.
- Log: na czerwono tylko crit/alert/emerg (Deco oznacza rutynę jako `daemon.err`), szum ukrywany także w logach
  przy incydentach (modem LTE, IGMP, klucze WiFi, wpisy firewalla).

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
