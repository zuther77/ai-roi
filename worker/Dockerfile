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
#   * /app/checkpoints : named volume (persistent model weights)
#   * /app/tracks      : the master's NFS export, mounted INSIDE the container
#                        by dell_worker.py at startup (Docker Desktop cannot
#                        pass a WSL-side NFS mount through as a bind mount)
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
    nfs-common \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/bin/python3 /usr/bin/python

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /opt/acestep

# Runtime code only. The pinned checkout is the single source of truth for
# versions (requirements.txt pins: diffusers>=0.33.0, pytorch_lightning==2.5.1,
# transformers==4.50.0, ...; torch comes from the cu126 extra index).
# README.md is required by setup.py itself (long_description), LICENSE for
# provenance. Missing README.md was the first build failure on DELL.
COPY requirements.txt setup.py README.md LICENSE ./
COPY acestep/ ./acestep/
COPY config/ ./config/

# Two layers: the big requirements download (torch cu126 is multi-GB) caches
# separately from `pip install .`, so a setup hiccup never re-downloads
# PyTorch.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir hf_transfer peft \
    && pip install --no-cache-dir -r requirements.txt \
        --extra-index-url https://download.pytorch.org/whl/cu126
RUN pip install --no-cache-dir .

# Runs as root: dell_worker.py mounts the master's NFS export into /app/tracks
# at startup, which needs CAP_SYS_ADMIN (run with --cap-add SYS_ADMIN). Root
# is contained by Docker; server-side, root_squash maps it to nobody (uid
# 65534) writing into the 1777 target dir — root inside the container has no
# privileged meaning on the master's filesystem.
RUN mkdir -p /app/checkpoints /app/tracks /app/logs

WORKDIR /app

CMD ["python", "/app/dell_worker.py"]
