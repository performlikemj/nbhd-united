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

COPY . .

# Download PII detection model (~554 MB FP32 safetensors) from HuggingFace at build time.
# Apache-2.0; lakshyakh93/deberta_finetuned_pii (DeBERTa-v3-base + ai4privacy).
# `ignore_patterns` skips the duplicate `pytorch_model.bin` — transformers
# loads the safetensors variant and the .bin would just double the layer size.
ENV PII_MODEL_PATH=/app/pii-model
# Retry with backoff — HuggingFace returns transient 429s that would otherwise
# kill the whole image build (and the deploy with it).
RUN for i in 1 2 3 4 5; do \
      python -c "from huggingface_hub import snapshot_download; snapshot_download('lakshyakh93/deberta_finetuned_pii', local_dir='/app/pii-model', ignore_patterns=['pytorch_model.bin', 'optimizer.pt', '*.msgpack', '*.h5'])" && exit 0; \
      echo "HF model download attempt $i failed; retrying in $((i*20))s..."; \
      sleep $((i*20)); \
    done; \
    echo "HF model download failed after 5 attempts" >&2; exit 1

RUN SECRET_KEY=build-placeholder python manage.py collectstatic --noinput

RUN chmod +x startup.sh

EXPOSE 8000

CMD ["./startup.sh"]
