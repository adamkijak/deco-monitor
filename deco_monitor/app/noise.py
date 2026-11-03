"""Wzorzec szumu w logu Deco (modem LTE, IGMP proxy, klucze WiFi, wpisy firewalla)."""

import re

NOISE_RE = re.compile(r"\[mobile\]|simplecom|IMPROXY|wpa_driver_nl80211_set_key|tp2\d\d,")
