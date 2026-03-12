FROM node:20-slim

WORKDIR /app

COPY proxy.js proxy-config-oc.json proxy-config-cc.json ./

ENV PROXY_LOG_PATH=/app/logs/proxy.log
ENV BIND_HOST=0.0.0.0

RUN mkdir -p /app/logs

EXPOSE 8899

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD node -e "require('http').get('http://localhost:8899', r => process.exit(r.statusCode === 200 ? 0 : 1))"

CMD ["node", "proxy.js"]
