# Threadfin (Python port). Build:  docker build -t threadfin-py .
# Run:  docker run -d -p 34400:34400 -v threadfin-config:/config threadfin-py
# The config folder (settings.json, xepg.json, data/, backup/) is /config; an existing ~/.threadfin
# from the Go app can be mounted there as-is.

ARG PYTHON_VERSION=3.12

FROM ghcr.io/astral-sh/uv:0.13.0 AS uv

# --- build: resolve the locked runtime dependencies and the app into /app/.venv ---------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src

# Dependencies first, so editing the code does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# --- runtime ----------------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm

# ffprobe (from ffmpeg) for the channel probe; streams themselves are 302 redirects, never transcoded.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 1000 threadfin \
    && useradd --system --uid 1000 --gid threadfin --home-dir /config --no-create-home threadfin \
    && mkdir /config && chown threadfin:threadfin /config

COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1

USER threadfin
VOLUME /config
EXPOSE 34400

# Assumes the default port; change it here too if settings.json uses another.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:34400/discover.json', timeout=4)"]

ENTRYPOINT ["threadfin", "-config", "/config"]
