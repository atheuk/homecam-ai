ARG BUILD_ARCH
FROM python:3.12-alpine

ARG BUILD_ARCH
# go2rtc >= 1.2.0 is required (PATCH /api/streams); 1.9.14 is the version
# verified against the adapter and the one Frigate 0.18 bundles.
ARG GO2RTC_VERSION=1.9.14

RUN apk add --no-cache ffmpeg curl \
    && case "${BUILD_ARCH}" in \
         aarch64) go2rtc_arch="arm64" ;; \
         armv7) go2rtc_arch="arm" ;; \
         amd64) go2rtc_arch="amd64" ;; \
         *) echo "Unsupported Home Assistant architecture: ${BUILD_ARCH}" >&2; exit 1 ;; \
       esac \
    && curl --fail --location --retry 3 \
         "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_${go2rtc_arch}" \
         -o /usr/local/bin/go2rtc \
    && chmod 755 /usr/local/bin/go2rtc

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py eufy_ws.py ./
COPY run.sh /run.sh
RUN chmod 755 /run.sh

CMD ["/run.sh"]
