FROM python:3.12-slim

WORKDIR /app

# System build deps (for cryptography etc.)
RUN apt-get update && apt-get install -y \
    build-essential \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates ./templates

ENV FLASK_APP=app.py
ENV FLASK_ENV=production

# On container start: init DB (safe if already exists), then start gunicorn
CMD ["sh", "-c", "flask --app app init-db && gunicorn -w 3 -b 0.0.0.0:5000 app:app"]
