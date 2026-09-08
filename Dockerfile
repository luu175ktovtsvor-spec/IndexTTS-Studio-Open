# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.12.5 AS uv

FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}" \
    HF_HOME=/app/checkpoints/.cache/huggingface \
    INDEXTTS_CHECKPOINTS_DIR=/app/checkpoints \
    INDEXTTS_STUDIO_HOST=0.0.0.0 \
    INDEXTTS_STUDIO_PORT=7860

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/

WORKDIR /app

RUN groupadd --gid 10001 indextts \
    && useradd --uid 10001 --gid 10001 --create-home indextts \
    && mkdir -p /app/checkpoints /app/outputs \
    && chown -R indextts:indextts /app

COPY --chown=indextts:indextts pyproject.toml uv.lock README.md LICENSE ./

USER indextts

RUN uv sync --frozen --no-dev --extra studio --no-install-project

COPY --chown=indextts:indextts . .

EXPOSE 7860

ENTRYPOINT ["./docker/entrypoint.sh"]
CMD ["python", "-m", "docker.run_studio"]
