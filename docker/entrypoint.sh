#!/usr/bin/env sh
set -e

mkdir -p data state

export PATH="/opt/agy:${PATH}"

# CMD "python -m bot.main" => loop with interval (container = scheduler)
# Any other CMD runs directly (one-shot: login, generate_profile, shell)
if [ "$1" = "python" ] && [ "$2" = "-m" ] && [ "$3" = "bot.main" ]; then
  interval_min="${SCRAPE_INTERVAL_MINUTES:-30}"
  while true; do
    echo "=== cycle start $(date -u +%FT%TZ) ==="
    python -m bot.main || echo "cycle failed, retrying in ${interval_min}min"
    echo "sleeping ${interval_min}min"
    sleep "$((interval_min * 60))"
  done
fi

exec "$@"
