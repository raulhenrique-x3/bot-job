#!/bin/sh
# Telegram alert when a systemd unit fails (OOM kill, timeout, non-zero exit).
# Usage: notify_failure.sh <failed-unit-name>
UNIT="$1"
ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env"
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
[ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ] || exit 0
curl -s --max-time 15 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  -d chat_id="${TELEGRAM_CHAT_ID}" \
  --data-urlencode "parse_mode=HTML" \
  --data-urlencode "disable_web_page_preview=true" \
  --data-urlencode "text=🚨 <b>bot-job: falha no systemd</b>
Unidade <code>${UNIT}</code> falhou no VPS.
Detalhes: <code>journalctl -u ${UNIT} -n 50</code>" >/dev/null 2>&1
