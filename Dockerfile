FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLCONFIGDIR=/tmp/matplotlib

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    fonts-dejavu-core \
    libpq-dev \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    postgresql-client \
    tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements.lock pyproject.toml ./
RUN python -m pip install -r requirements.lock

COPY . .
RUN mkdir -p /data/reports /data/uploads /data/backups /data/exports /data/error-reports /tmp/matplotlib \
    && chown -R 1000:1000 /data /app /tmp/matplotlib \
    && chmod +x /app/scripts/entrypoint.sh

USER 1000:1000
EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--", "/app/scripts/entrypoint.sh"]
CMD ["api"]
