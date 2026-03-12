FROM ubuntu:24.04

RUN apt-get update && \
    apt-get install -y --no-install-recommends cron curl sqlite3 jq gettext-base && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy lifecycle scripts
COPY scripts/ ./scripts/
RUN chmod +x ./scripts/*.sh

# Copy crontab template
COPY crontab.template ./

# Entrypoint: generate crontab from template, install, start cron
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

CMD ["./entrypoint.sh"]
