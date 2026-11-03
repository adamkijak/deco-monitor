#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
# Start add-onu: opcje z /data/options.json, archiwum w /share/deco_monitor, panel na porcie Ingress 8099.

export DECO_MONITOR_OPTIONS=/data/options.json
export DECO_MONITOR_DATA=/data
export DECO_MONITOR_SHARE=/share/deco_monitor
export DECO_MONITOR_WEB_PORT=8099

if ! bashio::services.available "mqtt" && ! bashio::config.has_value 'mqtt_host'; then
    bashio::log.warning "Brak brokera MQTT (zainstaluj dodatek Mosquitto broker) - encje nie pojawią się w HA"
fi

bashio::log.info "Start Deco Monitor"
exec python3 -u /app/main.py
