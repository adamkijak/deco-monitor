#!/usr/bin/env bash
# Uruchomienie dodatku na komputerze, bez Home Assistant i bez MQTT (publikacje tylko w logu).
# Panel: http://localhost:8099/   Dane: local/ w katalogu repozytorium.
#   DECO_PASSWORD=... [SWITCH_PASSWORD=...] [DECO_HOST=192.168.68.1] scripts/run_local.sh
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
work="${root}/local"
mkdir -p "${work}/data" "${work}/share"
: "${DECO_PASSWORD:?ustaw DECO_PASSWORD}"
python3 - "${root}/deco_monitor/config.yaml" "${work}/options.json" <<'PY'
import json, os, sys, yaml
opts = yaml.safe_load(open(sys.argv[1]))["options"]
opts["deco_password"] = os.environ["DECO_PASSWORD"]
opts["switch_password"] = os.environ.get("SWITCH_PASSWORD", "")
opts["deco_host"] = os.environ.get("DECO_HOST", opts["deco_host"])
json.dump(opts, open(sys.argv[2], "w"))
os.chmod(sys.argv[2], 0o600)
PY
export DECO_MONITOR_OPTIONS="${work}/options.json" DECO_MONITOR_DATA="${work}/data" \
       DECO_MONITOR_SHARE="${work}/share" DECO_MONITOR_DRY_RUN=1 DECO_MONITOR_WEB_PORT="${PORT:-8099}"
exec python3 -u "${root}/deco_monitor/app/main.py"
