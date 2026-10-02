FROM python:3.12-slim

LABEL org.opencontainers.image.title="pihole-sync" \
      org.opencontainers.image.description="Synchronise blocklists, whitelists, local DNS and settings from a primary Pi-hole (v6) to replicas" \
      org.opencontainers.image.source="https://github.com/yourname/pihole-sync" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pihole_sync ./pihole_sync

RUN mkdir -p /config /data

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request,sys; port=os.environ.get('PIHOLE_SYNC_PORT','8080'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/api/status', timeout=4).status==200 else 1)"

CMD ["python", "-m", "pihole_sync"]
