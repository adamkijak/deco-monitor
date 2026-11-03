"""Klasyfikacja linii logu Deco: szum, rutyna oznaczona jako "err" i wpisy istotne."""

import re

# Szum: modem LTE, proxy IGMP, ustawianie kluczy WiFi, wpisy firewalla (tp2xx jako user.emerg)
NOISE_RE = re.compile(r"\[mobile\]|simplecom|IMPROXY|wpa_driver_nl80211_set_key|tp2\d\d,")

# Rutyna, którą firmware zapisuje jako daemon.err (stderr usług w OpenWrt trafia do logu jako err)
ROUTINE_RE = re.compile(
    r"udhcpd\[\d+\]: (Sending (ACK|OFFER)|Received (DISCOVER|REQUEST|RELEASE)|clear ip|dhcp offer|lease ip|"
    r"Found static lease|my ntp server)"
    r"|client_mgmt: (wireless client associate or disassociate|client\(\S+\) (assocaite|disassociate) by|"
    r"mac \S+ had uploaded|client_mac:|fill_missed_re_client_list|client\(\S+\) disconnected|"
    r"client\(\S+\) not found in arp table disconnect_counter=[0-2]\b)"
    r"|uhttpd\[\d+\]: uci: Entry not found"
    r"|apsd: (apsd_check_eth_has_neigh|nl_wifi_newlink_handle|config_find_interface_by_index|apsd_get_active_ports)"
    r"|hostapd: hostapd_notif_disassoc num_sta"
    r"|nrd\[\d+\]: (estimatorDot11kIterateCB|wlanifBSteerEventsBufRdCB|estimator\w+: )"
    r"|: Error: connect$"
    r"|dnsproxy: \[ *\d+\] \[online_test_event_cb \d+\] nameserver isn't in the resolve.conf"
    r"|nrd\[\d+\]: ar_pat_fill_stainfo\[line \d+\]: Failed to find serving BSS"
    r"|awn\[\d+\]: \[update_wifi_tpie_qca:\d+\]: config_generic successed"
    r"|hostapd: wpa_auth_sm_event do WPA_LAST_STA"
    r"|miniupnpd\[\d+\]: Why did you run me anyway"
    r"|nrd\[\d+\]: steeralg\w+"
    r"|uhttpd\[\d+\]: cat: write error: Broken pipe"
)

# Wpisy, które dodatek rozpoznaje jako zdarzenia (lub ich bezpośredni kontekst)
RELEVANT_RE = re.compile(
    r"br-wan|conn-indicator|smartip|mobile_backup: (ewan|mobile_enable)|ssh_connect|AP-STA-|Lease of|"
    r"Interface 'wanb?' (is now|has link)|syslogd started|Booting Linux|disconnect_counter=([3-9]|\d\d)"
)

_LEVEL_RE = re.compile(r"^\S+ \S+ +\d+ \S+ \d{4} [a-z]+\.([a-z]+) ")


def is_important(line: str) -> bool:
    """Do filtra "tylko istotne": zdarzenia rozpoznawane przez dodatek, prawdziwe crit/alert/emerg
    oraz err/warn spoza znanej rutyny (czyli coś nowego, czego jeszcze nie znamy)."""
    if NOISE_RE.search(line):
        return False
    if RELEVANT_RE.search(line):
        return True
    if ROUTINE_RE.search(line):
        return False
    m = _LEVEL_RE.match(line)
    return (m.group(1) if m else "") in ("crit", "alert", "emerg", "err", "warning", "warn")
