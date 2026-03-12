FROM node:20-slim

WORKDIR /app

# Install curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Copy package files and install dependencies
COPY package.json ./
RUN npm install --production

# Copy application files
COPY server.js init-db.js semantic-dedup.js schema.sql anthropic-client.js agents.json ./
COPY migrations/ ./migrations/

# Create data directory
RUN mkdir -p /app/data

# Environment defaults
ENV BIND_HOST=0.0.0.0
ENV PORT=8897
ENV DB_PATH=/app/data/memory.db
ENV EMBEDDING_SERVICE_URL=http://embedding-service:8896

EXPOSE 8897

# Init DB if it doesn't exist, then start server
CMD ["sh", "-c", "test -f $DB_PATH || node init-db.js && node server.js"]
