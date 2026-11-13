FROM python:3.14-slim

WORKDIR /srv
# HF_HOME is where the embedding model lives. The compose hf_cache volume mounts
# this same path so the baked copy is reused (and persists across recreates).
ENV PYTHONPATH=/srv UV_PROJECT_ENVIRONMENT=/srv/.venv HF_HOME=/srv/hf
ENV HF_HUB_DISABLE_TELEMETRY=1

COPY pyproject.toml uv.lock ./
# Dependencies: Linux torch resolves to the CPU-only wheel via [tool.uv.sources]
# (embeddings are CPU work); the CUDA build would add ~2.5 GB of unused libs.
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev

# Bake the default embedding model in at build time. A clean clone otherwise
# downloads it at runtime, and that cold load can exceed TOOL_TIMEOUT_S and
# escalate the very first ticket of a fresh deployment.
RUN /srv/.venv/bin/python -c \
    "from sentence_transformers import SentenceTransformer as S; S('BAAI/bge-small-en-v1.5')"

COPY app ./app
COPY db ./db
COPY config ./config
# The dev-only admin routes import these, and the image is what a clean clone
# runs: without them POST /api/admin/{seed,ingest-kb,load-tickets} 500s.
COPY scripts ./scripts
COPY data ./data
COPY eval ./eval

EXPOSE 8000
CMD ["/srv/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
