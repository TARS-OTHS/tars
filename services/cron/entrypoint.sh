#!/bin/bash
set -e

# Generate crontab from template with environment variables
envsubst < /app/crontab.template > /etc/cron.d/tars-cron
chmod 644 /etc/cron.d/tars-cron
crontab /etc/cron.d/tars-cron

# Export env vars for cron jobs
printenv | grep -E '^(TARS_|MEMORY_|EMBEDDING_|DOCKER_|AUTH_PROXY|PRIMARY_)' > /app/.env.cron
chmod 600 /app/.env.cron

echo "TARS cron service started"

# Run cron in foreground
exec cron -f
