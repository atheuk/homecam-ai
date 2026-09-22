ARG BUILD_ARCH
FROM python:3.12-alpine

ARG BUILD_ARCH
ARG MEDIAMTX_VERSION=1.15.1

RUN apk add --no-cache curl tar \
    && case "${BUILD_ARCH}" in \
         aarch64) mediamtx_arch="arm64" ;; \
         armv7) mediamtx_arch="armv7" ;; \
         amd64) mediamtx_arch="amd64" ;; \
         *) echo "Unsupported Home Assistant architecture: ${BUILD_ARCH}" >&2; exit 1 ;; \
       esac \
    && curl --fail --location --retry 3 \
         "https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/mediamtx_v${MEDIAMTX_VERSION}_linux_${mediamtx_arch}.tar.gz" \
         -o /tmp/mediamtx.tar.gz \
    && tar -xzf /tmp/mediamtx.tar.gz -C /usr/local/bin mediamtx \
    && rm /tmp/mediamtx.tar.gz

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY homecam-edge/run.sh /run.sh
RUN chmod 755 /run.sh

CMD ["/run.sh"]
