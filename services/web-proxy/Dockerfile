FROM ubuntu:24.04

RUN apt-get update && \
    apt-get install -y --no-install-recommends tinyproxy curl && \
    rm -rf /var/lib/apt/lists/*

COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf

EXPOSE 8899

CMD ["tinyproxy", "-d"]
