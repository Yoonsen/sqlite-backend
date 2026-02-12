FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libsqlite3-dev \
  && rm -rf /var/lib/apt/lists/*

COPY api_python/requirements.txt /app/api_python/requirements.txt
RUN pip install --no-cache-dir -r /app/api_python/requirements.txt

COPY api_python /app/api_python
COPY postings.c /app/postings.c
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh

ENV POSTINGS_CONFIG=/data/dhlab/larsj/postings/config.json
ENV POSTINGS_SO_PATH=/data/dhlab/larsj/postings/postings_native.so

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
