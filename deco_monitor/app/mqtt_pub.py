"""Publikacja encji do Home Assistant przez MQTT discovery."""

from __future__ import annotations

import json
import logging
import re
import threading
import unicodedata

log = logging.getLogger("mqtt")

BASE = "deco_monitor"
DISCOVERY = "homeassistant"
AVAIL = f"{BASE}/status"

_PL = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")


def slugify(text: str) -> str:
    """Jak homeassistant.util.slugify (python-slugify, separator "_")."""
    text = unicodedata.normalize("NFKD", text.translate(_PL)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def is_stale_discovery_topic(topic: str) -> bool:
    """homeassistant/<domena>/deco_monitor/<domena>.deco_monitor_x/config - zły temat z wersji <= 1.3.4."""
    parts = topic.split("/")
    return (len(parts) == 5 and parts[0] == DISCOVERY and parts[2] == BASE and parts[4] == "config"
            and "." in parts[3])


class Publisher:
    def __init__(self, host: str | None, port: int, username: str | None, password: str | None,
                 version: str, dry_run: bool = False):
        self.dry_run = dry_run or not host
        self.version = version
        self._entities: dict = {}  # uid -> (domain, config)
        self._retained: dict = {}  # topic -> payload
        self._lock = threading.Lock()
        self._cleanup_timer = None
        self.device = {
            "identifiers": [BASE],
            "name": "Deco Monitor",
            "manufacturer": "Deco Monitor (dodatek HA)",
            "model": "Monitor sieci Deco + TL-SG108E",
            "sw_version": version,
        }
        self.client = None
        if self.dry_run:
            log.warning("MQTT wyłączone (tryb testowy) - publikacje tylko w logu")
            return
        import paho.mqtt.client as mqtt

        try:  # paho-mqtt 2.x wymaga wskazania wersji API callbacków
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="deco_monitor",
                                      clean_session=True)
        except AttributeError:  # paho-mqtt 1.x
            self.client = mqtt.Client(client_id="deco_monitor", clean_session=True)
        if username:
            self.client.username_pw_set(username, password)
        self.client.will_set(AVAIL, "offline", retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(1, 60)
        log.info("Łączenie z brokerem MQTT %s:%s", host, port)
        self.client.connect_async(host, port, keepalive=60)
        self.client.loop_start()

    # -- połączenie ------------------------------------------------------
    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.error("MQTT odrzuciło połączenie (rc=%s)", rc)
            return
        log.info("Połączono z MQTT")
        client.subscribe(f"{DISCOVERY}/status")
        # wersje <= 1.3.4 przy ponownej publikacji używały złego tematu (z "domena." w object_id);
        # takie retained kopie odtwarzały w HA encje, które już usunięto - czyścimy je
        client.subscribe(f"{DISCOVERY}/+/{BASE}/+/config")
        self._republish()

    def _on_message(self, client, userdata, msg):
        if is_stale_discovery_topic(msg.topic) and msg.payload:
            log.info("Usuwam starą kopię konfiguracji MQTT: %s", msg.topic)
            self._raw(msg.topic, "", retain=True)
            # gdyby HA utworzył encję właśnie z tej kopii, usunięcie kopii usunie encję - publikujemy
            # więc wszystko jeszcze raz pod właściwymi tematami (raz, po zebraniu wszystkich kopii)
            with self._lock:
                if self._cleanup_timer is None:
                    self._cleanup_timer = threading.Timer(10, self._republish)
                    self._cleanup_timer.daemon = True
                    self._cleanup_timer.start()
            return
        if msg.topic == f"{DISCOVERY}/status" and msg.payload == b"online":
            log.info("Home Assistant wystartował - ponowna publikacja encji")
            self._republish()

    def _republish(self):
        with self._lock:
            ents = list(self._entities.items())
            retained = dict(self._retained)
        for _key, (domain, cfg) in ents:
            self._raw(f"{DISCOVERY}/{domain}/{BASE}/{cfg['unique_id']}/config", json.dumps(cfg), retain=True)
        self._raw(AVAIL, "online", retain=True)
        for topic, payload in retained.items():
            self._raw(topic, payload, retain=True)

    def _raw(self, topic: str, payload: str, retain: bool = False):
        if self.dry_run:
            log.debug("PUB %s %s", topic, payload[:160])
            return
        self.client.publish(topic, payload, qos=0, retain=retain)

    # -- encje -----------------------------------------------------------
    def entity(self, domain: str, name: str, attrs: bool = False, **extra) -> str:
        """Rejestruje encję; zwraca identyfikator używany w state()/attrs().
        entity_id w HA = {domain}.deco_monitor_{slugify(name)}"""
        slug = slugify(name)
        uid = f"{BASE}_{slug}"
        topic = f"{BASE}/{domain}/{slug}"
        cfg = {
            "name": name,
            "unique_id": uid,
            "default_entity_id": f"{domain}.{uid}",
            "availability_topic": AVAIL,
            "device": self.device,
            "state_topic": f"{topic}/state",
        }
        if attrs:
            cfg["json_attributes_topic"] = f"{topic}/attr"
        if domain == "binary_sensor":
            cfg.update({"payload_on": "ON", "payload_off": "OFF"})
        cfg.update({k: v for k, v in extra.items() if v is not None})
        key = f"{domain}.{uid}"
        with self._lock:
            new = key not in self._entities or self._entities[key][1] != cfg
            self._entities[key] = (domain, cfg)
        if new:
            self._raw(f"{DISCOVERY}/{domain}/{BASE}/{uid}/config", json.dumps(cfg), retain=True)
        return key

    def remove(self, key: str) -> None:
        """Usuwa encję z HA (pusty retained config) i czyści jej retained state/attr."""
        with self._lock:
            item = self._entities.pop(key, None)
            if item is None:
                return
            domain, cfg = item
            base = cfg["state_topic"].rsplit("/", 1)[0]
            topics = [f"{base}/state", f"{base}/attr"]
            for t in topics:
                self._retained.pop(t, None)
        self._raw(f"{DISCOVERY}/{domain}/{BASE}/{cfg['unique_id']}/config", "", retain=True)
        for t in topics:
            self._raw(t, "", retain=True)

    def _topic(self, key: str, suffix: str) -> str:
        domain, cfg = self._entities[key]
        return cfg["state_topic"].rsplit("/", 1)[0] + "/" + suffix

    def state(self, key: str, value, retain: bool = True):
        if value is None:
            return
        if isinstance(value, bool):
            value = "ON" if value else "OFF"
        payload = value if isinstance(value, str) else json.dumps(value)
        topic = self._topic(key, "state")
        if retain:
            with self._lock:
                self._retained[topic] = payload
        self._raw(topic, payload, retain=retain)

    def attrs(self, key: str, data: dict, retain: bool = True):
        payload = json.dumps(data, ensure_ascii=False, default=str)
        topic = self._topic(key, "attr")
        if retain:
            with self._lock:
                self._retained[topic] = payload
        self._raw(topic, payload, retain=retain)

    def entity_ids(self) -> list:
        with self._lock:
            return sorted(self._entities)


def supervisor_mqtt() -> dict:
    """Dane brokera z Supervisora (dodatek z `services: mqtt:want`)."""
    import os
    import urllib.request

    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return {}
    req = urllib.request.Request("http://supervisor/services/mqtt",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("data", {}) or {}
    except Exception as exc:  # brak usługi MQTT albo brak dostępu
        log.warning("Nie udało się pobrać konfiguracji MQTT z Supervisora: %s", exc)
        return {}
