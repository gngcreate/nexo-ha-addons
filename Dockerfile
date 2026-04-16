FROM python:3.12-alpine

# Instalar dependencias del sistema
RUN apk add --no-cache --virtual .build-deps gcc musl-dev libffi-dev openssl-dev \
    && apk add --no-cache libffi openssl

# Instalar Python packages
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && apk del .build-deps

# Copiar archivos
COPY rootfs /
COPY run.sh /run.sh
RUN chmod a+x /run.sh

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 -c "import sys; sys.exit(0)" || exit 1

CMD ["/run.sh"]
