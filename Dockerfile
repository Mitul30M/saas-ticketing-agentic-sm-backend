FROM python:3.14-slim

WORKDIR /srv
ENV PYTHONPATH=/srv UV_PROJECT_ENVIRONMENT=/srv/.venv

COPY pyproject.toml uv.lock ./
# Dependencies: Linux torch resolves to the CPU-only wheel via [tool.uv.sources]
# (embeddings are CPU work); the CUDA build would add ~2.5 GB of unused libs.
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev

COPY app ./app
COPY db ./db
COPY config ./config

EXPOSE 8000
CMD ["/srv/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
