ARG BUILD_ARCH=amd64
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base-python:3.12-alpine3.20

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/nexo-agent

# Dependencias para compilar paquetes Python en arquitecturas sin wheel.
RUN apk add --no-cache --virtual .build-deps \
    build-base \
    python3-dev \
    libffi-dev \
    openssl-dev

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --upgrade pip setuptools wheel \
    && pip3 install --no-cache-dir --prefer-binary -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt \
    && apk del .build-deps

# Copiar archivos
COPY rootfs /
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
