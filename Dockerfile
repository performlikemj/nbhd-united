FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=config.settings.production

WORKDIR /app

# Older Debian releases bundle legacy aliases in tzdata and have no
# tzdata-legacy package, so install the split package only when available.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    ffmpeg \
    tzdata \
    && if apt-cache show tzdata-legacy >/dev/null 2>&1; then \
        apt-get install -y --no-install-recommends tzdata-legacy; \
    fi \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# torch's default PyPI wheel pulls ~1 GB of CUDA runtime libs we never use
# (production runs CPU-only on Container Apps). Install the CPU build first
# from the pytorch CPU index so the `-r requirements.txt` step finds torch
# already satisfied and skips the NVIDIA-bundled wheel.
# Read the version FROM requirements.txt so a Dependabot bump can never desync
# this pin again: that skew once reinstalled torch from PyPI (CUDA-bloated) and
# corrupted transformers (ImportError on AutoModelForTokenClassification).
RUN TORCH_PIN="$(grep -E '^torch==' requirements.txt)" && \
    pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
    "$TORCH_PIN"
RUN pip install --no-cache-dir -r requirements.txt

# PII detection models (DeBERTa-v3 + Liquid FP32). Pulled as one frozen layer
# from our own ACR — NOT from HuggingFace — so deploys never hit HF rate limits
# (429s used to kill the build). The immutable tag is derived as
# `<shape>-<first 16 chars of DeBERTa revision>-<first 16 chars of Liquid
# revision>`. CI builds it once, then reuses it; bump the tag THERE and HERE
# together whenever either pinned model or the bundle shape changes.
# Placed before `COPY . .` so app-code changes never invalidate this layer.
# Keep production serving offline: a missing baked model must fail during worker
# warm-up instead of downloading hundreds of MB inside the readiness window.
ENV PII_DETECTOR_ENGINE=deberta \
    PII_MODEL_PATH=/app/pii-model \
    HF_HUB_OFFLINE=1
COPY --from=nbhdunited.azurecr.io/pii-model:deberta-liquid-a038061af92047b0-b8c9cf3d2d6ae525 /pii-model /app/pii-model

COPY . .

RUN SECRET_KEY=build-placeholder python manage.py collectstatic --noinput

RUN chmod +x startup.sh

EXPOSE 8000

CMD ["./startup.sh"]
