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

    def test_ping_keeps_home(self):
        pings = []
        def ping(ip):
            pings.append(ip)
            return True
        p = Presence(["OnePlus-13"], away_after=300, ping_window=1800)
        p.update([PHONE], 1000)
        self.assertEqual(p.update([OTHER], 1200, ping)["OnePlus-13"][0], "home")
        self.assertEqual(pings, [])  # w oknie 5 min bez pingu
        out = p.update([OTHER], 1300, ping)["OnePlus-13"]
        self.assertEqual(out[0], "home")
        self.assertEqual(pings, ["192.168.68.87"])
        self.assertEqual(out[1]["odpowiada_na_ping"], 1300)
        self.assertEqual(p.update([OTHER], 1500, ping)["OnePlus-13"][0], "home")
        self.assertEqual(len(pings), 1)  # odpowiedź przedłuża o 5 min, bez kolejnego pingu
        self.assertEqual(p.update([OTHER], 2800, ping)["OnePlus-13"][0], "not_home")  # po 30 min nie pingujemy
        self.assertEqual(p.update([PHONE], 2830, ping)["OnePlus-13"][1].get("odpowiada_na_ping"), None)

    def test_ping_no_answer(self):
        p = Presence(["OnePlus-13"], away_after=300)
        p.update([PHONE], 1000)
        self.assertEqual(p.update([OTHER], 1300, lambda ip: False)["OnePlus-13"][0], "not_home")

    def test_survives_restart(self):
        p = Presence(["OnePlus-13"], away_after=300)
        p.update([PHONE], 1000)
        q = Presence(["OnePlus-13", "OnePlus-15"], away_after=300)
        q.load(p.export())
        out = q.update([OTHER], 1100)
        self.assertEqual(out["OnePlus-13"][0], "home")
        self.assertEqual(out["OnePlus-13"][1]["ip"], "192.168.68.87")
        self.assertEqual(out["OnePlus-15"][0], "not_home")
        r = Presence(["Inny"])
        r.load(p.export())
        self.assertEqual(r.last_seen, {})
        r.load(None)

    def test_offline_client_ignored(self):
        p = Presence(["OnePlus-13"])
        self.assertEqual(p.update([{**PHONE, "online": False}], 1000)["OnePlus-13"][0], "not_home")

    def test_empty_names(self):
        self.assertEqual(Presence(None).update([PHONE], 1), {})
        self.assertEqual(Presence(["", "  "]).names, [])

    def test_tracker_without_availability(self):
        from mqtt_pub import Publisher
        pub = Publisher(None, 1883, None, None, "test")
        key = pub.entity("device_tracker", "Obecność X", source_type="router", no_availability=True)
        self.assertNotIn("availability_topic", pub._entities[key][1])
        self.assertNotIn("no_availability", pub._entities[key][1])
        key = pub.entity("sensor", "Coś")
        self.assertIn("availability_topic", pub._entities[key][1])

    def test_entity_slug(self):
        self.assertEqual(slugify("Obecność OnePlus-13"), "obecnosc_oneplus_13")


if __name__ == "__main__":
    unittest.main()
