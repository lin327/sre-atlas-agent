FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN groupadd --system app \
    && useradd --system --gid app --create-home --home-dir /home/app \
        --shell /usr/sbin/nologin app \
    && mkdir -p /app/data /app/output \
    && chown -R app:app /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app . .

USER app:app

# This is a one-shot batch worker; its process exit code reports job health.
# A long-running service HEALTHCHECK is not applicable.
CMD ["python", "-m", "agent.main", "--once"]
