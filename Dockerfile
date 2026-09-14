# syntax=docker/dockerfile:1

FROM node:22-alpine AS frontend-builder

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build:static


FROM python:3.12-slim AS api

ARG RELAY_UID=998
ARG RELAY_GID=998
ARG DEBIAN_MIRROR=https://deb.debian.org/debian
ARG DEBIAN_SECURITY_MIRROR=https://deb.debian.org/debian-security

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RELAY_DB_PATH=/var/lib/relay-api/relay.db \
    RELAY_BIND_HOST=0.0.0.0 \
    RELAY_BIND_PORT=18777

RUN sed -i "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g; s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates openssh-client rsync util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system relay \
    && useradd --system --gid relay --home-dir /var/lib/relay-api --create-home --shell /usr/sbin/nologin relay

# Use a stable UID/GID for persistent volume ownership and SSH user lookup.
RUN groupmod --gid ${RELAY_GID} relay \
    && usermod --uid ${RELAY_UID} --gid relay relay

WORKDIR /app
COPY server/relay_api.py server/relay_transfer.py ./

RUN chown -R relay:relay /app /var/lib/relay-api

USER relay
EXPOSE 18777

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18777/relay/api/health', timeout=3)"

CMD ["python", "/app/relay_api.py"]


# Default production image: static frontend and API in one non-root container.
FROM api AS relay

USER root
RUN apt-get update \
    && apt-get install --no-install-recommends -y nginx-light supervisor tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=frontend-builder /app/static-dist /usr/share/nginx/html/relay
COPY deploy/nginx.single.conf /etc/nginx/nginx.conf
COPY deploy/supervisord.conf /etc/relay/supervisord.conf
COPY deploy/container-healthcheck.py /app/container-healthcheck.py
COPY deploy/container-failure-listener.py /app/container-failure-listener.py

ENV RELAY_BIND_HOST=127.0.0.1
USER relay
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python /app/container-healthcheck.py

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/bin/supervisord", "-c", "/etc/relay/supervisord.conf"]
