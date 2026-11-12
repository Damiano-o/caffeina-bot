FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Europe/Rome

WORKDIR /app

COPY requirements-docker.txt ./
RUN pip install --upgrade pip && \
    pip install -r requirements-docker.txt && \
    python -m playwright install --with-deps chromium && \
    rm -rf /var/lib/apt/lists/*

COPY caffeina_bot ./caffeina_bot
COPY run.py config.toml ./

RUN mkdir -p data
VOLUME ["/app/data"]

# Il bot lavora solo su Telegram: nessuna porta esposta
CMD ["python", "run.py", "run"]
