FROM python:3.12-slim

WORKDIR /app

# System libraries Chromium needs to actually run (fonts, graphics libs, etc.)
# playwright install --with-deps installs these via apt on Debian/Ubuntu.
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && crawl4ai-setup

COPY . .

# .mo files are build output, so they are compiled here rather than committed.
# A language whose catalogue is still untranslated compiles to an empty one and
# renders English, which is what makes it safe to deploy a partial translation.
RUN pybabel compile -d locales || true

ENV PYTHONUNBUFFERED=1

# $PORT is honoured if the host injects one, defaulting to 8000 otherwise.
#
# --proxy-headers makes Starlette trust X-Forwarded-Proto/Host from whatever
# terminates TLS in front of the container, so OAuth redirect URLs come out as
# https:// against the real hostname rather than http:// against an internal one.
#
# --workers 1 is required, not just a default: this app keeps crawl/job state in
# in-process memory (see app/job_store.py), so more than one worker means
# requests get routed to processes that don't share that state. Explicit here
# because uvicorn otherwise falls back to $WEB_CONCURRENCY if it's set, which
# many hosts set automatically based on instance size.
#
# See DEPLOY.md for the rest of the requirements — persistent volume, health
# check path, and the OAuth redirect URI needed per hostname.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
