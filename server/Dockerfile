FROM python:3.12-slim

ARG RCLONE_VERSION=v1.68.2
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl unzip tzdata \
 && arch="$(dpkg --print-architecture)" \
 && curl -fsSL "https://downloads.rclone.org/${RCLONE_VERSION}/rclone-${RCLONE_VERSION}-linux-${arch}.zip" -o /tmp/rclone.zip \
 && unzip -q /tmp/rclone.zip -d /tmp \
 && install -m 0755 /tmp/rclone-*/rclone /usr/local/bin/rclone \
 && rm -rf /tmp/rclone* \
 && apt-get purge -y unzip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN useradd -u 1000 -m app && mkdir -p /data /config/rclone && chown -R app /data /config

ENV DATA_DIR=/data RCLONE_CONFIG=/config/rclone/rclone.conf PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
