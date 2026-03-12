FROM python:3.11-slim

WORKDIR /app

# Install curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY embedding-service.py ./

# Create models directory (downloaded on first run)
RUN mkdir -p /app/models

# Environment defaults
ENV BIND_HOST=0.0.0.0
ENV PORT=8896
ENV MODEL_DIR=/app/models

EXPOSE 8896

CMD ["python", "embedding-service.py"]
