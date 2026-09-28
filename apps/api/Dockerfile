FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
# opencv-python-headless (AI_DETECTOR_BACKEND=opencv, SPEC 13) still dynamically
# links against a couple of shared libs the slim base image doesn't ship, even
# though the "headless" wheel itself needs no GUI/media codecs.
RUN apt-get update && apt-get install --no-install-recommends -y libglib2.0-0 libgl1 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt
# Bake the RT-DETR detection model into the image (Apache-2.0 code *and*
# weights, upstream PekingU/rtdetr_r18vd). Doing this at build time rather
# than on first request keeps startup deterministic and means a running
# container never needs outbound access to huggingface.co. The INT8 export is
# ~21MB and scores within 0.001 of FP32 on COCO control images.
ARG RTDETR_MODEL_URL=https://huggingface.co/onnx-community/rtdetr_r18vd/resolve/main/onnx/model_quantized.onnx
RUN mkdir -p /app/models \
    && python -c "import urllib.request,os,sys; \
u=os.environ.get('RTDETR_MODEL_URL') or sys.argv[1]; \
urllib.request.urlretrieve(u,'/app/models/rtdetr.onnx')" "$RTDETR_MODEL_URL" \
    && python -c "import os; s=os.path.getsize('/app/models/rtdetr.onnx'); \
assert s > 10_000_000, f'RT-DETR model looks truncated: {s} bytes'; \
print(f'RT-DETR model OK: {s} bytes')"
COPY app app
COPY alembic alembic
COPY alembic.ini .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
