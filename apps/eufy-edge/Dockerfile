# Small footprint image: this runs on a Home Assistant host / Raspberry Pi
# (arm64/armv7) next to the eufy-security-ws bridge.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY eufy_ws.py app.py ./

EXPOSE 8091
# Bind on all interfaces: the *host* Docker/Tailscale networking, not this
# container, is what keeps this reachable only over the private overlay
# network -- see docs/eufy.md.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8091"]
