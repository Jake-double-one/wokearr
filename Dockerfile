FROM python:3.12-slim

# DejaVu font for the badge label (Pillow needs a TrueType font)
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

ENV DATA_DIR=/data \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]

EXPOSE 5005

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5005/healthz')" || exit 1

# Only 1 worker process: job progress (cache build/applying badges) lives in
# an in-memory dict in app.py and must therefore stay in a single process,
# otherwise some polling requests land in the wrong process ("unknown" status).
# --threads still provides concurrency within that one process.
CMD ["gunicorn", "--bind", "0.0.0.0:5005", "--workers", "1", "--threads", "4", "--timeout", "120", "app:app"]
