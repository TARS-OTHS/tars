FROM python:3.11-slim

WORKDIR /app

# Install curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY api-server.py index.html favicon.svg ./
COPY static/ ./static/

# Environment defaults
ENV BIND_HOST=0.0.0.0
ENV PORT=8765
ENV API_PORT=8766
ENV MEMORY_API_URL=http://memory-api:8897
ENV OPENCLAW_BIN=/usr/local/bin/openclaw

EXPOSE 8765 8766

CMD ["python", "api-server.py"]
