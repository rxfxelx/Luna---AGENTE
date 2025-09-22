# syntax=docker/dockerfile:1

# Base image with Python installed.  Use the slim variant to keep
# the image small.  Alpine could be used but building some
# dependencies can be more complicated.
FROM python:3.11-slim

LABEL maintainer="Luna Backend Team <noreply@example.com>"

# Set a working directory inside the container
WORKDIR /app

# Copy and install Python dependencies first.  By copying only
# requirements.txt initially we leverage Docker layer caching: if
# requirements.txt hasn't changed, Docker will reuse the layer
# containing the installed packages.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the remaining application files into the container
COPY fastapi_app ./fastapi_app
COPY .env.example ./

# Expose the port the app runs on.  Railway will map this to its own
# generated port automatically.
ENV PORT=8000
EXPOSE 8000

# Set the default command to start the FastAPI application.  Uvicorn
# is used as the ASGI server.  The ``--proxy-headers`` flag tells
# Uvicorn to respect X‑Forwarded‑* headers which is often important
# when behind a reverse proxy.  ``--host`` and ``--port`` bind the
# server to all interfaces on the configured port.
CMD ["uvicorn", "fastapi_app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]