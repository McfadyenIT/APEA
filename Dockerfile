# APEA — Autonomous Performance Engineering Agent
# Multi-purpose image: runs the web UI by default, or the headless CLI for CI.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps kept minimal; locust wheels are self-contained.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8000

# Default: launch the web app. Override the command to run the CLI, e.g.:
#   docker run --rm apea python -m apea.cli --url https://example.com --check-sla
CMD ["python", "run.py", "--host", "0.0.0.0", "--no-browser"]
