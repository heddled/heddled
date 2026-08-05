# Heddled — self-hosted first (concept principle 5).
#
# One image runs the whole platform: console, JSON API, SSE trace stream, and
# the background worker that drains the turn queue and ticks pull triggers.
# Split the worker out with `command: heddled worker` if you want two processes.

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HEDDLED_ROOT=/app \
    HEDDLED_HOST=0.0.0.0 \
    HEDDLED_PORT=5005

WORKDIR /app

# Dependencies first so an edit to the source does not invalidate this layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md ./
COPY heddled ./heddled
RUN pip install --no-cache-dir --no-deps .

# agents/ and tools/ are bind-mounted in compose so `git diff` stays the truth;
# these directories exist for the case where the image is run standalone.
RUN mkdir -p /app/agents /app/tools /app/data /app/var

EXPOSE 5005

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:5005/api/health', timeout=4).status == 200 else 1)"

CMD ["heddled", "serve"]
