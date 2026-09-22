# Small footprint image: this runs on a Raspberry Pi (arm64/armv7).
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

EXPOSE 8443
# Bind on all interfaces: the *host* Docker/Tailscale networking, not this
# container, is what keeps this reachable only over the private overlay
# network -- see docs/edge-connector.md.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8443"]
