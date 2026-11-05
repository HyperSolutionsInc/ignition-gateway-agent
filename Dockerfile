# hyper gateway: Ignition 8.3 and gateway-agent in one image. Start it and it sets itself up:
# Ignition commissions itself, the agent mints its own Ignition API key, provisions its own AWS
# IoT identity from the fleet claim certificate, and installs the IMSs the cloud assigns it.
#
#   docker build -t hyper-gateway:0.2.1 \
#     --build-arg IOT_ENDPOINT=<account>-ats.iot.us-east-2.amazonaws.com \
#     --build-arg IOT_PROVISIONING_TEMPLATE=<template> .
#
# The claim certificate and key go in docker/claim/ before building (git-ignored).
FROM inductiveautomation/ignition:8.3.7

ARG IOT_ENDPOINT=a27n1c51bedgc5-ats.iot.us-east-2.amazonaws.com
ARG IOT_PROVISIONING_TEMPLATE=hyper-gateway-fleet

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/gateway-agent/src
COPY pyproject.toml requirements.txt ./
COPY gateway_agent ./gateway_agent
RUN python3 -m venv /opt/gateway-agent/venv \
 && /opt/gateway-agent/venv/bin/pip install --no-cache-dir -r requirements.txt \
 && /opt/gateway-agent/venv/bin/pip install --no-cache-dir --no-deps . \
 && rm -rf /root/.cache

COPY docker/certs/AmazonRootCA1.pem /opt/gateway-agent/certs/AmazonRootCA1.pem
COPY docker/claim/ /opt/gateway-agent/claim/
COPY --chmod=0755 docker/gateway-entrypoint.sh /usr/local/bin/gateway-entrypoint.sh
RUN rm -f /opt/gateway-agent/claim/README.md \
 && chown -R 2003:2003 /opt/gateway-agent/claim \
 && chmod 0700 /opt/gateway-agent/claim && (chmod 0600 /opt/gateway-agent/claim/* 2>/dev/null || true) \
 && mkdir -p /var/lib/gateway-agent && chown 2003:2003 /var/lib/gateway-agent && chmod 0700 /var/lib/gateway-agent

ENV ACCEPT_IGNITION_EULA=Y \
    IGNITION_EDITION=standard \
    GATEWAY_ADMIN_USERNAME=admin \
    IGNITION_URL=http://127.0.0.1:8088 \
    IGNITION_DATA_DIR=/usr/local/bin/ignition/data \
    AGENT_STATE_DIR=/var/lib/gateway-agent \
    AGENT_HTTP_HOST=127.0.0.1 \
    AGENT_URL_FOR_IGNITION=http://127.0.0.1:8600 \
    IOT_ENDPOINT=${IOT_ENDPOINT} \
    IOT_PROVISIONING_TEMPLATE=${IOT_PROVISIONING_TEMPLATE} \
    IOT_CA_PATH=/opt/gateway-agent/certs/AmazonRootCA1.pem \
    PATH=/opt/gateway-agent/venv/bin:/usr/local/bin/ignition/lib/runtime/jre/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

USER 2003:2003
WORKDIR /usr/local/bin/ignition
VOLUME ["/usr/local/bin/ignition/data", "/var/lib/gateway-agent"]
EXPOSE 8088 8043
ENTRYPOINT ["tini", "-g", "--", "/usr/local/bin/gateway-entrypoint.sh"]
