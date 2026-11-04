FROM python:3.12-slim

# Run as the same uid/gid as the Ignition image so project files written into the shared
# data volume are owned the way the gateway expects.
ARG APP_UID=2003
ARG APP_GID=2003
RUN groupadd -g ${APP_GID} ignition && useradd -u ${APP_UID} -g ${APP_GID} -M -s /usr/sbin/nologin agent

WORKDIR /app
COPY pyproject.toml ./
COPY gateway_agent ./gateway_agent
RUN pip install --no-cache-dir . && rm -rf /root/.cache

RUN mkdir -p /var/lib/gateway-agent && chown ${APP_UID}:${APP_GID} /var/lib/gateway-agent
VOLUME ["/var/lib/gateway-agent"]
USER ${APP_UID}:${APP_GID}

EXPOSE 8600
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8600/healthz', timeout=4)" || exit 1

ENTRYPOINT ["gateway-agent"]
CMD ["run"]
