FROM python:3.12-slim

WORKDIR /app

# curl is used by the compose healthcheck against /healthz. ffmpeg supplies
# both ffmpeg and ffprobe, which ext/render.py shells out to.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install uv (build-time only; the runtime uses the venv python directly)
ADD https://astral.sh/uv/install.sh /uv-installer.sh
RUN sh /uv-installer.sh && rm /uv-installer.sh
ENV PATH="/root/.local/bin:${PATH}"

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies with sync
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Copy application code
COPY main.py oauth.py r2.py media.py files.py ./
COPY src/ ./src/
COPY ext/ ./ext/

# Bake the rembg model into the image. Fetched lazily it is a ~170 MB download
# on the first cutout after every cold start, since container disk is ephemeral.
ENV U2NET_HOME=/app/.u2net
RUN /app/.venv/bin/python -c "from rembg import new_session; new_session('isnet-general-use')"

# Run as a non-root user (this service is exposed to the internet)
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/chats /app/files \
    && chown -R appuser:appuser /app
USER appuser

# HTTP transport listens here (MCP_PORT). Reached via caddy or cloudflared,
# never published to the host directly.
EXPOSE 8000

# Invoke the venv interpreter directly rather than `uv run`: appuser cannot
# write uv's cache under /root, and re-resolving the lock at boot buys nothing.
CMD ["/app/.venv/bin/python", "main.py"]
