FROM python:3.11-slim

WORKDIR /app

# Install system dependencies (age for vault decryption)
RUN apt-get update && apt-get install -y --no-install-recommends \
    age \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY src/ .

# Environment defaults
ENV TARS_HOME=/tars
ENV DOCKER_HOST_IP=172.17.0.1
ENV AUTH_PROXY_PORT=9100
ENV BIND_HOST=0.0.0.0

EXPOSE 9100

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen("http://localhost:9100/ops/health")"

CMD ["python", "auth-proxy.py"]
