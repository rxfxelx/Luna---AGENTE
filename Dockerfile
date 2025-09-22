# syntax=docker/dockerfile:1

FROM python:3.11-slim

LABEL maintainer="Luna Backend Team <noreply@example.com>"

# 1) Sistema: instalar CA bundle para validação TLS (necessário p/ Postgres/HTTPs)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && update-ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Diretório de trabalho
WORKDIR /app

# 2) Dependências Python (camada cacheável)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3) Código da aplicação
COPY fastapi_app ./fastapi_app
COPY .env.example ./

# 4) Porta (Railway mapeia automaticamente)
ENV PORT=8000
EXPOSE 8000

# 5) Comando
CMD ["uvicorn", "fastapi_app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
