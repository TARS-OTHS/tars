# TARS Troubleshooting Guide

Common issues encountered during deployment and operation, with solutions.

---

## Service Won't Start

### auth-proxy: "could not bind on any address"

**Symptom:**
```
OSError: could not bind on any address out of [('172.17.0.1', 9100)]
```

**Cause:** The auth-proxy was trying to bind to the Docker host gateway IP (`172.17.0.1`) which is not available inside the container.

**Fix:** The `BIND_HOST` environment variable must be `0.0.0.0` inside the container. This is set in `docker-compose.yml` — if you see this error, ensure the auth-proxy service has:
```yaml
environment:
  - BIND_HOST=0.0.0.0
```
The port mapping in `docker-compose.yml` (`172.17.0.1:9100:9100`) handles restricting access to the Docker network.

---

### auth-proxy / credential-proxy: "Failed to decrypt vault"

**Symptom:**
```
ERROR Failed to decrypt vault: age: error: failed to open input file "/opt/tars/.secrets-vault/secrets.age": no such file or directory
```
or
```
FATAL: Failed to decrypt vault: Command failed: age -d ...
```

**Cause:** The age-encrypted vault file (`secrets.age`) hasn't been created yet. This happens on fresh deployments before `setup.sh` has run the integrations step.

**Fix:** Both services now handle this gracefully — they start with empty secrets and log a warning. If you see the warning but the service is healthy, this is expected on a fresh install. The vault gets created when you configure integrations via `setup.sh` Section 5.

To verify the service is running despite the warning:
```bash
docker compose ps auth-proxy
docker compose ps credential-proxy
```

---

### credential-proxy: "age: not found"

**Symptom:**
```
/bin/sh: 1: age: not found
```

**Cause:** The `age` binary is not installed in the credential-proxy container.

**Fix:** The credential-proxy Dockerfile should include `age` in its apt-get install step. However, with the graceful vault handling fix, this error is now avoided entirely — the service checks for file existence before attempting decryption.

---

### dashboard: healthcheck failing / "Connection refused"

**Symptom:** Dashboard shows as `unhealthy` in `docker compose ps`, healthcheck logs show `ConnectionRefusedError`.

**Cause:** The API server listens on port 8766 but the healthcheck was targeting port 8765.

**Fix:** Ensure the healthcheck in `docker-compose.yml` targets the correct port:
```yaml
healthcheck:
  test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8766/')"]
```

---

## Database Issues

### memory-api: "no such table: memories"

**Symptom:** Memory API returns 500 errors, logs show SQLite "no such table" errors.

**Cause:** The SQLite database file exists (from a Docker volume) but was never initialized with the schema.

**Fix:** The entrypoint script (`entrypoint.sh`) now checks for the `memories` table on startup and runs `init-db.js` if it's missing. If you hit this on an older version:
```bash
docker compose exec memory-api node init-db.js
```

Or reset completely:
```bash
docker compose down memory-api
docker volume rm tars_memory-data
docker compose up -d memory-api
```

---

## Embedding Service

### embedding-service: slow first startup

**Symptom:** The embedding service takes 60-90 seconds on first startup, healthcheck shows `starting` for a while.

**Cause:** The BGE-small-en-v1.5 ONNX model (~130MB) is downloaded on first run.

**Fix:** This is expected behaviour. The model is cached in the `embedding-models` Docker volume and subsequent startups are fast. The healthcheck has a `start_period: 60s` to account for this.

If the download fails (network issues):
```bash
docker compose restart embedding-service
```

### embedding-service: "Could not find ONNX model"

**Symptom:**
```
FileNotFoundError: Could not find ONNX model file
```

**Cause:** The model download completed but the file layout doesn't match what the code expects (flat vs `onnx/` subdirectory).

**Fix:** The service now checks both `model.onnx` (flat) and `onnx/model.onnx` (subdirectory) layouts. If you still see this, clear the volume:
```bash
docker compose down embedding-service
docker volume rm tars_embedding-models
docker compose up -d embedding-service
```

---

## Cron Service

### cron: "envsubst: not found"

**Symptom:** Cron container fails to start, logs show `envsubst: not found`.

**Cause:** The `gettext-base` package (which provides `envsubst`) was missing from the cron Dockerfile.

**Fix:** The cron Dockerfile now includes `gettext-base` in its apt-get install step. If running an older image:
```bash
docker compose build --no-cache cron
docker compose up -d cron
```

---

## Docker Compose

### "version is obsolete" warning

**Symptom:**
```
WARN[0000] /opt/tars/docker-compose.yml: the attribute `version` is obsolete
```

**Cause:** The `version: '3.8'` key in docker-compose.yml is deprecated in modern Docker Compose.

**Fix:** Remove the `version` line from `docker-compose.yml`. It's no longer needed.

---

## Network Issues

### Services can't reach each other

**Symptom:** Services return connection errors when trying to reach other services (e.g., memory-api can't reach embedding-service).

**Fix:** All services must be on the same Docker network. Check that every service in `docker-compose.yml` includes:
```yaml
networks:
  - tars
```

And that the network is defined:
```yaml
networks:
  tars:
    driver: bridge
```

Verify connectivity:
```bash
docker compose exec memory-api curl -s http://embedding-service:8896/health
```

---

## General Debugging

### Check service logs
```bash
# All services
docker compose logs --tail 50

# Specific service
docker compose logs --tail 50 auth-proxy

# Follow logs in real-time
docker compose logs -f memory-api
```

### Check service health
```bash
docker compose ps
```

### Restart a single service
```bash
docker compose restart auth-proxy
```

### Full rebuild (nuclear option)
```bash
docker compose down -v           # Stop everything, delete volumes
docker compose build --no-cache  # Rebuild all images from scratch
docker compose up -d             # Start fresh
```

### Verify endpoints manually
```bash
curl http://172.17.0.1:8897/status          # memory-api
curl http://172.17.0.1:8896/health          # embedding-service
curl http://172.17.0.1:9100/ops/health      # auth-proxy
curl http://localhost:8766/health           # dashboard
```
