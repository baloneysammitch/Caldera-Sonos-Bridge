FROM python:3.14-slim-trixie@sha256:35caba83af2fe6da9a5d922f9b0156f21d1246a48278c58a08bd253303ba42e8
LABEL org.opencontainers.image.title="Caldera Sonos Bridge security fork" \
      org.opencontainers.image.source="https://github.com/baloneysammitch/Caldera-Sonos-Bridge" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 CONFIG_DIR=/config \
    HTTP_PORT=32700 PLAYER_PORT_BASE=32701 SETTINGS_BIND=127.0.0.1
WORKDIR /app
# Refresh the vendor trust store without adding runtime administration tools.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY calderabridge ./calderabridge
COPY LICENSE ./LICENSE
RUN useradd --system --uid 10001 --no-create-home bridge \
    && mkdir -p /config && chown 10001:10001 /config
USER 10001:10001
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://'+os.environ['SETTINGS_BIND']+':'+os.environ['HTTP_PORT']+'/status.json',timeout=8).read()"
ENTRYPOINT ["python", "-m", "calderabridge"]
