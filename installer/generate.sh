#!/usr/bin/env bash
# installer/generate.sh — Config generation helpers
# Sourced by setup.sh

generate_auth_proxy_routes() {
    local config_dir="$SCRIPT_DIR/config"
    mkdir -p "$config_dir"
    cat > "$config_dir/auth-proxy-routes.json" << ROUTESEOF
{
  "routes": {
    "anthropic": {
      "upstream": "https://api.anthropic.com",
      "auth": "x-api-key",
      "secret_key": "ANTHROPIC_API_KEY",
      "enabled": true
    },
    "openai": {
      "upstream": "https://api.openai.com",
      "auth": "bearer",
      "secret_key": "OPENAI_API_KEY",
      "enabled": ${OPENAI_API_KEY:+true}${OPENAI_API_KEY:-false}
    },
    "tavily": {
      "upstream": "https://api.tavily.com",
      "auth": "bearer",
      "secret_key": "TAVILY_API_KEY",
      "enabled": ${TAVILY_API_KEY:+true}${TAVILY_API_KEY:-false}
    },
    "notion": {
      "upstream": "https://api.notion.com",
      "auth": "bearer",
      "secret_key": "NOTION_TOKEN",
      "enabled": ${NOTION_TOKEN:+true}${NOTION_TOKEN:-false}
    },
    "trello": {
      "upstream": "https://api.trello.com",
      "auth": "query",
      "secret_keys": ["TRELLO_KEY", "TRELLO_TOKEN"],
      "enabled": ${TRELLO_KEY:+true}${TRELLO_KEY:-false}
    },
    "google": {
      "upstream": "https://www.googleapis.com",
      "auth": "oauth2",
      "enabled": ${GOOGLE_CLIENT_ID:+true}${GOOGLE_CLIENT_ID:-false}
    }
  }
}
ROUTESEOF
    print_success "Auth proxy routes config written"
}

encrypt_secrets_to_vault() {
    local pubkey="$1"
    local vault_path="$2"
    local tmp_secrets
    tmp_secrets=$(mktemp)
    chmod 600 "$tmp_secrets"

    # Write secrets to temp file
    {
        [[ -n "${ANTHROPIC_API_KEY:-}" ]] && echo "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}"
        [[ -n "${OPENAI_API_KEY:-}" ]]    && echo "OPENAI_API_KEY=${OPENAI_API_KEY}"
        [[ -n "${TAVILY_API_KEY:-}" ]]    && echo "TAVILY_API_KEY=${TAVILY_API_KEY}"
        [[ -n "${NOTION_TOKEN:-}" ]]      && echo "NOTION_TOKEN=${NOTION_TOKEN}"
        [[ -n "${TRELLO_KEY:-}" ]]        && echo "TRELLO_KEY=${TRELLO_KEY}"
        [[ -n "${TRELLO_TOKEN:-}" ]]      && echo "TRELLO_TOKEN=${TRELLO_TOKEN}"
        [[ -n "${DISCORD_BOT_TOKEN:-}" ]] && echo "DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN}"
        [[ -n "${SLACK_BOT_TOKEN:-}" ]]   && echo "SLACK_BOT_TOKEN=${SLACK_BOT_TOKEN}"
        [[ -n "${GOOGLE_CLIENT_ID:-}" ]]  && echo "GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID}"
        [[ -n "${GOOGLE_CLIENT_SECRET:-}" ]] && echo "GOOGLE_CLIENT_SECRET=${GOOGLE_CLIENT_SECRET}"
    } > "$tmp_secrets"

    mkdir -p "$(dirname "$vault_path")"
    age -r "$pubkey" -o "$vault_path" "$tmp_secrets"
    rm -f "$tmp_secrets"
    print_success "Secrets encrypted to vault"
}

generate_openclaw_config() {
    local config_path="$TARS_HOME/.openclaw/openclaw.json"
    mkdir -p "$(dirname "$config_path")"

    cat > "$config_path" << OCEOF
{
  "version": "1",
  "bind": "lan",
  "port": 18789,
  "agents": {
    "defaults": {
      "model": "${AGENT_MODEL}",
      "sandbox": {
        "docker": {
          "image": "openclaw-sandbox:latest",
          "env": {
            "http_proxy": "http://${DOCKER_HOST_IP}:${WEB_PROXY_PORT:-8899}",
            "https_proxy": "http://${DOCKER_HOST_IP}:${WEB_PROXY_PORT:-8899}",
            "no_proxy": "${DOCKER_HOST_IP},localhost,127.0.0.1",
            "ANTHROPIC_API_URL": "http://${DOCKER_HOST_IP}:${AUTH_PROXY_PORT:-9100}/anthropic"
          }
        }
      }
    },
    "list": [
      {
        "id": "${AGENT_ID}",
        "name": "${AGENT_NAME}",
        "model": "${AGENT_MODEL}",
        "workspace": "${TARS_HOME}/workspace-${AGENT_ID}",
        "platform": "${MESSAGING_PLATFORM}",
        "enabled": true
      }
    ]
  }
}
OCEOF
    print_success "OpenClaw config written"
}
