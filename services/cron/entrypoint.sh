#!/bin/bash
set -e

# Generate crontab from template with environment variables
envsubst < /app/crontab.template > /etc/cron.d/tars-cron
chmod 644 /etc/cron.d/tars-cron
crontab /etc/cron.d/tars-cron

# Export env vars for cron jobs (cron doesn't inherit environment)
printenv | grep -E '^(TARS_|MEMORY_|EMBEDDING_|DOCKER_|AUTH_PROXY|PRIMARY_|OPENCLAW_|OC_|SECRETS_|OPS_ALERTS|AGENT_)' > /app/.env.cron
chmod 600 /app/.env.cron

# Create log file
touch /var/log/tars-cron.log

echo "TARS cron service started ($(date -u +%Y-%m-%dT%H:%M:%SZ))"

# Run cron in foreground
exec cron -f
