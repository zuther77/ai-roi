# Worker container — SLIM since the ACE-Step 1.5 migration.
#
# The model now lives in the separate "acestep15" container, built from
# ACE-Step-1.5's own Dockerfile at the pinned checkout (their CUDA 12.8.1 /
# Python 3.11 / uv build — build commands in worker/README.md). Running the
# API server and the worker as separate containers keeps their image
# pristine and lets the model restart without killing our health endpoint
# or claim loop.
#
# This image therefore carries no torch and no model — just:
#   * python + the stdlib worker (NFS mount, Redis claim loop, REST client)
#   * nfs-common: the worker mounts the master's export INSIDE this
#     container (Docker Desktop cannot pass a WSL-side NFS mount through)
#   * fastapi/uvicorn for the worker's own /health endpoint
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends nfs-common \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir fastapi uvicorn

WORKDIR /app

# No code baked in: the whole worker/ directory (dell_worker.py,
# ace_client.py, base.py) is bind-mounted read-only at run time, so code
# changes never need an image rebuild.
CMD ["python", "/app/worker/dell_worker.py"]
