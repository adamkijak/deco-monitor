ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.20
FROM ${BUILD_FROM}

# python3 + biblioteki z pakietów Alpine (bez kompilacji przy instalacji):
#   cryptography - protokół RSA/AES lokalnego API Deco
#   paho-mqtt    - encje w Home Assistant przez MQTT discovery
#   pyyaml       - generowanie dashboardu
# iputils - pełny `ping` (busybox nie zna -n)
RUN apk add --no-cache python3 py3-cryptography py3-paho-mqtt py3-yaml iputils

COPY app/ /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
