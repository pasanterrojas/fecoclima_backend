#!/usr/bin/env sh
set -eu

MODE="${1:-api}"

case "$MODE" in
  migrate)
    alembic upgrade head
    python -m app.scripts.init_db
    ;;
  api)
    exec uvicorn app.main:app \
      --host 0.0.0.0 \
      --port "${API_PORT:-8000}" \
      --workers "${WEB_CONCURRENCY:-2}" \
      --proxy-headers \
      --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}"
    ;;
  worker)
    exec python -m app.worker
    ;;
  scheduler)
    exec python -m app.scheduler
    ;;
  *)
    exec "$@"
    ;;
esac
