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

# PII detection model (~554 MB, DeBERTa-v3 + ai4privacy, Apache-2.0). Pulled as a
# frozen layer from our own ACR — NOT from HuggingFace — so deploys never hit HF
# rate limits (429s used to kill the build). The pii-model:<tag> image is built
# once by the "Ensure PII model image" step in .github/workflows/ci-cd.yml; bump
# the tag THERE and HERE together when changing the model. See Dockerfile.pii-model.
# Placed before `COPY . .` so app-code changes never invalidate this layer.
# Keep production serving offline: a missing baked model must fail during worker
# warm-up instead of downloading hundreds of MB inside the readiness window.
ENV PII_MODEL_PATH=/app/pii-model \
    HF_HUB_OFFLINE=1
COPY --from=nbhdunited.azurecr.io/pii-model:deberta-finetuned-pii-v2 /pii-model /app/pii-model

COPY . .

RUN SECRET_KEY=build-placeholder python manage.py collectstatic --noinput

RUN chmod +x startup.sh

EXPOSE 8000

CMD ["./startup.sh"]
