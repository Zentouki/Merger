# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MERGER_HOST=0.0.0.0 \
    MERGER_DATA=/data \
    TMPDIR=/data/tmp

WORKDIR /app

# Dependencies first, so this layer is cached until requirements.txt changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Run as an unprivileged user; /data holds everything that must survive updates
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin merger \
 && mkdir -p /data/tmp \
 && chown -R merger:merger /data

COPY app.py index.html login.html ./

USER merger
VOLUME ["/data"]
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/login', timeout=3)"

# app.py starts Waitress (a production WSGI server) when it is installed
CMD ["python", "app.py"]
