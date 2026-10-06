FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_DATA_DIR=/data \
    TZ=America/Toronto

# tzdata so TZ works (the budget month and "today" follow your local time).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Runs as a normal user, never root. On a bind mount, docker-compose sets `user:`
# to match the owner of the data folder.
RUN useradd --system --uid 10001 --no-create-home app \
 && mkdir /data && chown app /data
USER app

EXPOSE 8080 8081
VOLUME /data

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/health', timeout=4)" || exit 1

CMD ["python", "-m", "app.main"]
