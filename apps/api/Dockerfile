# --- Stage 1: build the anydoc (Rust) Python wheel -------------------------
# The anydoc source arrives as a named build context ("anydoc") set by
# docker-compose (additional_contexts), so the wheel is built from the exact
# fork we maintain without copying it into this repository.
FROM rust:1.96-slim AS anydoc-builder
RUN apt-get update && apt-get install -y --no-install-recommends python3-pip && rm -rf /var/lib/apt/lists/*
ARG ANYDOC_SRC=/
COPY --from=anydoc ${ANYDOC_SRC} /build/anydoc
RUN pip install --no-cache-dir --break-system-packages maturin \
    && cd /build/anydoc/python && maturin build --release --out /wheels \
    && ls /wheels

# --- Stage 2: runtime --------------------------------------------------------
FROM python:3.12-slim
# poppler-utils: anydoc shells out to pdftoppm to render OCR pages.
# tesseract-ocr + tesseract-ocr-tha: the chain's last-resort local engine.
RUN apt-get update && apt-get install -y --no-install-recommends \
        poppler-utils tesseract-ocr tesseract-ocr-tha \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=anydoc-builder /wheels/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl
# The build context is the repository root (see docker-compose x-api-build).
COPY apps/api/pyproject.toml .
COPY apps/api/app ./app
COPY apps/api/migrations ./migrations
COPY apps/api/alembic.ini ./
RUN pip install --no-cache-dir .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
