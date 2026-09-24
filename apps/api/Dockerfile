FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
# opencv-python-headless (AI_DETECTOR_BACKEND=opencv, SPEC 13) still dynamically
# links against a couple of shared libs the slim base image doesn't ship, even
# though the "headless" wheel itself needs no GUI/media codecs.
RUN apt-get update && apt-get install --no-install-recommends -y libglib2.0-0 libgl1 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY alembic alembic
COPY alembic.ini .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
