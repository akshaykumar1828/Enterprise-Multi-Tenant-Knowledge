# FastAPI + retrieval models. Build context: the project root (see compose.yaml).
#
# The image uses the CPU build of PyTorch: the embedding model and the reranker are small
# and fast enough on CPU, and the image stays portable (no CUDA runtime, no GPU setup).
# Written answers come from Ollama on the host, which keeps using the GPU.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/hf

WORKDIR /app

# Same pinned versions as requirements.txt, except torch comes from the CPU index.
COPY requirements.txt .
RUN torch_version="$(sed -nE 's/^torch==([0-9.]+).*/\1/p' requirements.txt)" \
 && pip install "torch==${torch_version}" --index-url https://download.pytorch.org/whl/cpu \
 && grep -vE '^(torch==|--extra-index-url)' requirements.txt > /tmp/requirements.txt \
 && pip install -r /tmp/requirements.txt

# Download both models at build time; at runtime HF_HUB_OFFLINE=1 keeps the API offline.
RUN python -c "from sentence_transformers import CrossEncoder, SentenceTransformer; \
SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2'); \
CrossEncoder('cross-encoder/ms-marco-MiniLM-L6-v2')"

COPY src ./src

# Unprivileged user; the uploads volume is created from this folder, so it inherits the owner.
RUN useradd --system --uid 10001 --no-create-home app \
 && mkdir -p /data/uploads \
 && chown app /data/uploads
USER app

ENV APP_ENV=production \
    HF_HUB_OFFLINE=1 \
    UPLOAD_DIR=/data/uploads

EXPOSE 8000

# One worker (models are loaded per worker). Only the web container can reach this port,
# so the forwarded client IP from Caddy is trusted.
CMD ["python", "-m", "uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--no-server-header", "--no-access-log"]
