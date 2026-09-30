FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

WORKDIR /app

COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install -r requirements.txt \
    && playwright install chromium \
    && playwright install-deps chromium

COPY bot ./bot
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && useradd -m -u 1000 botuser \
    && chown -R botuser /app \
    && chown -R botuser /opt/ms-playwright

USER botuser

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "bot.main"]
