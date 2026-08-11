FROM ghcr.io/open-webui/open-webui:v0.11.0 AS base
WORKDIR /service
COPY app.py /service/app.py
ENV PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false \
    OMP_NUM_THREADS=4 \
    MKL_NUM_THREADS=4 \
    KNOWLEDGE_MAX_TEXT_CHARS_PER_FILE=500000 \
    KNOWLEDGE_SWEEP_SECONDS=300 \
    KNOWLEDGE_WATCH_REFRESH_SECONDS=15 \
    KNOWLEDGE_WATCH_POLL_MS=5000 \
    KNOWLEDGE_ORPHAN_GRACE_SECONDS=300

FROM base AS test
RUN pip install --no-cache-dir pytest==9.0.3
COPY tests /service/tests
CMD ["python", "-m", "pytest", "-q", "tests"]

FROM base AS runtime
EXPOSE 8100
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8100"]
