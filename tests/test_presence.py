"""python3 -m unittest discover tests"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deco_monitor", "app"))

from mqtt_pub import slugify  # noqa: E402
from presence import Presence  # noqa: E402

PHONE = {"name": "OnePlus-13", "mac": "02-00-00-5C-A2-77", "ip": "192.168.68.87",
         "connection_type": "band5", "wire_type": "wireless", "online": True}
OTHER = {"name": "Galaxy-Tab", "mac": "02-00-00-00-00-01", "ip": "192.168.68.79", "online": True}


class PresenceTest(unittest.TestCase):
    def test_home_when_on_list(self):
        p = Presence(["OnePlus-13", "OnePlus-15"])
        out = p.update([PHONE, OTHER], 1000)
        self.assertEqual(out["OnePlus-13"][0], "home")
        self.assertEqual(out["OnePlus-13"][1]["ip"], "192.168.68.87")
        self.assertEqual(out["OnePlus-13"][1]["pasmo"], "5 GHz")
        self.assertEqual(out["OnePlus-15"][0], "not_home")
        self.assertNotIn("ostatnio_widziany", out["OnePlus-15"][1])

    def test_name_case_insensitive_and_mac_change(self):
        p = Presence([" oneplus-13 "])
        out = p.update([{**PHONE, "name": "ONEPLUS-13", "mac": "02-11-22-33-44-55"}], 1000)
        self.assertEqual(out["oneplus-13"][0], "home")
        self.assertEqual(out["oneplus-13"][1]["mac"], "02-11-22-33-44-55")

    def test_away_only_after_delay(self):
        p = Presence(["OnePlus-13"], away_after=300)
        p.update([PHONE], 1000)
        out = p.update([OTHER], 1299)
        self.assertEqual(out["OnePlus-13"][0], "home")  # krótkie zniknięcie z WiFi
        self.assertFalse(out["OnePlus-13"][1]["na_liscie"])
        self.assertEqual(out["OnePlus-13"][1]["ip"], "192.168.68.87")  # ostatnie znane dane
        self.assertEqual(out["OnePlus-13"][1]["ostatnio_widziany"], 1000)
        self.assertEqual(p.update([OTHER], 1300)["OnePlus-13"][0], "not_home")
        back = p.update([PHONE], 1330)["OnePlus-13"]
        self.assertEqual(back[0], "home")
        self.assertNotIn("ostatnio_widziany", back[1])  # na liście: atrybuty stałe między odczytami

    def test_offline_client_ignored(self):
        p = Presence(["OnePlus-13"])
        self.assertEqual(p.update([{**PHONE, "online": False}], 1000)["OnePlus-13"][0], "not_home")

    def test_empty_names(self):
        self.assertEqual(Presence(None).update([PHONE], 1), {})
        self.assertEqual(Presence(["", "  "]).names, [])

    def test_entity_slug(self):
        self.assertEqual(slugify("Obecność OnePlus-13"), "obecnosc_oneplus_13")


if __name__ == "__main__":
    unittest.main()
