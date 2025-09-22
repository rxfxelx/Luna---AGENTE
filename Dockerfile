# syntax=docker/dockerfile:1

FROM python:3.11-slim

LABEL maintainer="Luna Backend Team <noreply@example.com>"

# 1) CA bundle para TLS (Postgres/HTTPs)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && update-ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 2) Dependências Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3) Código
COPY fastapi_app ./fastapi_app
COPY .env.example ./

ENV PORT=8000
EXPOSE 8000

CMD ["uvicorn", "fastapi_app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
