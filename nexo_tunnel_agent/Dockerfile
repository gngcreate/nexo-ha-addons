ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.20
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/nexo-agent

COPY requirements.txt /tmp/requirements.txt
RUN if ! command -v python3 >/dev/null 2>&1 || ! command -v pip >/dev/null 2>&1; then \
      apk add --no-cache python3 py3-pip; \
    fi \
 && python3 -m pip install --no-cache-dir -r /tmp/requirements.txt

COPY rootfs /
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
