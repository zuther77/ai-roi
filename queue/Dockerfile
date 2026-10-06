# Day 8 queue manager. Code is mounted at /app/queue so a Python edit does
# not require a rebuild; this image only exists to carry psycopg.
FROM python:3.12-slim

RUN pip install --no-cache-dir "psycopg[binary]"

WORKDIR /app
CMD ["python", "-u", "/app/queue/manager.py"]
