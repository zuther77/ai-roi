# Day 5 — DELL generation worker (detailed-plan Day 5, tasks 4-5).
#
# Extends ace-step/ACE-Step's own Dockerfile (pinned at commit
# 1bee4c9f5b43e30995f8d4d33b3919197ce1bd68): same CUDA 12.6 runtime base,
# same venv + requirements (cu126 wheels). Differences:
#   * the build context is the ACE-Step CHECKOUT, not this repo — the source
#     is on disk already (owner-supplied, commit-pinned), so no network clone
#     inside the build and nothing floats on `main`
#   * only the runtime code is copied (acestep/, config/) — trainer, datasets,
#     examples and assets stay out of the image
#   * no GUI: the container runs our queue worker, bind-mounted at runtime
#     (so worker-code changes never need an image rebuild)
#
# Build, from the ACE-Step checkout directory (adjust the -f path to wherever
# this repo lives beside it):
#   cd /path/to/ACE-Step
#   docker build -f ../ai-roi/worker/Dockerfile -t ai-roi-worker .
#
# Runtime expectations (see worker/README.md for the full run command):
#   * GPU: run with --gpus all (Docker Desktop WSL2 backend on Windows)
#   * /app/checkpoints : bind-mounted persistent model weights
#   * /app/tracks      : the master's NFS export, mounted via WSL2
#   * /app/dell_worker.py : this repo's worker script, bind-mounted read-only

FROM nvidia/cuda:12.6.0-runtime-ubuntu22.04

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_ENABLE_HF_TRANSFER=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.10 \
    python3-pip \
    python3-venv \
    python3-dev \
    build-essential \
    git \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/bin/python3 /usr/bin/python

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN useradd -m -u 1001 appuser

WORKDIR /opt/acestep

# Runtime code only. The pinned checkout is the single source of truth for
# versions (requirements.txt pins: diffusers>=0.33.0, pytorch_lightning==2.5.1,
# transformers==4.50.0, ...; torch comes from the cu126 extra index).
COPY requirements.txt setup.py ./
COPY acestep/ ./acestep/
COPY config/ ./config/

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir hf_transfer peft \
    && pip install --no-cache-dir -r requirements.txt \
        --extra-index-url https://download.pytorch.org/whl/cu126 \
    && pip install --no-cache-dir .

RUN mkdir -p /app/checkpoints /app/tracks /app/logs \
    && chown -R appuser:appuser /app

USER appuser
WORKDIR /app

CMD ["python", "/app/dell_worker.py"]
