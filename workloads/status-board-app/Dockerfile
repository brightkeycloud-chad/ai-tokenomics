# Runs on Fargate. Build for the architecture the task definition asks for:
#   docker build --platform linux/arm64 -t status-board .
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run as a non-root user. The container needs no write access to its own files.
RUN useradd --create-home --shell /usr/sbin/nologin board \
    && chown -R board:board /srv
USER board

EXPOSE 8080

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
