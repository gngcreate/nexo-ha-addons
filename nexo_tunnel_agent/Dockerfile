ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.20
FROM ${BUILD_FROM}

# Version: 2
ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/nexo-agent

COPY requirements.txt /tmp/requirements.txt
RUN apk add --no-cache python3 py3-pip && \
    pip3 install --no-cache-dir -r /tmp/requirements.txt

COPY rootfs /
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
