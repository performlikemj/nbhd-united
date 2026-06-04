FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=config.settings.production

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# torch's default PyPI wheel pulls ~1 GB of CUDA runtime libs we never use
# (production runs CPU-only on Container Apps). Install the CPU build first
# from the pytorch CPU index so the `-r requirements.txt` step finds torch
# already satisfied and skips the NVIDIA-bundled wheel.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
    torch==2.12.0
RUN pip install --no-cache-dir -r requirements.txt

# PII detection model (~554 MB, DeBERTa-v3 + ai4privacy, Apache-2.0). Pulled as a
# frozen layer from our own ACR — NOT from HuggingFace — so deploys never hit HF
# rate limits (429s used to kill the build). The pii-model:<tag> image is built
# once by the "Ensure PII model image" step in .github/workflows/ci-cd.yml; bump
# the tag THERE and HERE together when changing the model. See Dockerfile.pii-model.
# Placed before `COPY . .` so app-code changes never invalidate this layer.
ENV PII_MODEL_PATH=/app/pii-model
COPY --from=nbhdunited.azurecr.io/pii-model:deberta-finetuned-pii-v1 /pii-model /app/pii-model

COPY . .

RUN SECRET_KEY=build-placeholder python manage.py collectstatic --noinput

RUN chmod +x startup.sh

EXPOSE 8000

CMD ["./startup.sh"]
