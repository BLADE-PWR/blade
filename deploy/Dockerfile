# One Dockerfile for every server service (--build-arg SERVICE=<name>).
FROM python:3.12-slim

ARG SERVICE
ENV SERVICE=${SERVICE} \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1

# OpenCV (detector) needs libGL and glib even when headless
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11.2 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY libs libs
COPY services services
RUN test -n "$SERVICE" && uv sync --frozen --no-dev --package blade-${SERVICE}

CMD ["sh", "-c", "exec blade-${SERVICE}"]
