FROM python:3.13-slim-trixie@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81

ARG TARGETARCH
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WACLI_STORE_DIR=/data/store \
    HOME=/data

COPY install-wacli.py /tmp/install-wacli.py
RUN python /tmp/install-wacli.py "${TARGETARCH}" \
    && rm /tmp/install-wacli.py \
    && mkdir -p /data/store

COPY healthcheck.py /usr/local/lib/whatsapp/healthcheck.py

USER 1000:1000
WORKDIR /data
ENTRYPOINT ["wacli", "--store", "/data/store"]
CMD ["sync", "--follow", "--download-media", "--presence-mode", "quiet", "--stale-threshold", "2m", "--max-reconnect", "5m", "--refresh-contacts", "--refresh-groups"]
