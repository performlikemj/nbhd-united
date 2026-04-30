FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=config.settings.production

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Download PII detection model (ONNX INT8, ~230 MB) from HuggingFace at build time.
# Public repo — no token needed. Model cached in image layer.
ENV PII_MODEL_PATH=/app/pii-model
RUN python -c "\
from huggingface_hub import snapshot_download; \
snapshot_download('onbekend/nbhd-pii-model', local_dir='/app/pii-model')"

RUN SECRET_KEY=build-placeholder python manage.py collectstatic --noinput

RUN chmod +x startup.sh

EXPOSE 8000

CMD ["./startup.sh"]
