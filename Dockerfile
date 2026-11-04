# trading-bots in a container. The project folder (config/, .env, data/, logs/) is mounted at /data.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZ=Asia/Kolkata
# git: the dhanbot dependency is installed from its repository (pinned commit)
RUN apt-get update && apt-get install -y --no-install-recommends git tzdata && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[redis]"

RUN useradd --create-home --uid 1000 trader && mkdir -p /data && chown trader /data
USER trader
WORKDIR /data
EXPOSE 8501
ENTRYPOINT ["bots", "--dir", "/data"]
CMD ["run", "--all"]
