FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /uvx /usr/local/bin/

RUN rm -f /etc/apt/sources.list.d/yarn.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*