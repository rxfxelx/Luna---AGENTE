# syntax=docker/dockerfile:1

FROM python:3.11-slim

LABEL maintainer="Luna Backend Team <noreply@example.com>"

# Instala pacote de certificados (necessário para conexões SSL)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && update-ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copia e instala dependências
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia código e env de exemplo
COPY fastapi_app ./fastapi_app
COPY .env.example ./

ENV PORT=8000
EXPOSE 8000

CMD ["uvicorn", "fastapi_app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
