# Official Lambda Web Adapter 1.1.0 (linux/amd64), taken from
# public.ecr.aws/awsguru/aws-lambda-adapter:1.1.0. GitHub runners cannot pull
# that registry (anonymous 429). A binary compiled in this image crashed on
# Lambda before Django started, so the function URL returned 502. This copy is
# the static release binary (sha256 358b36689e564ba479aba244916ba5226e18d7d01f4bf6bf164088552a71c40d).
# Local Compose does not invoke the extension.
FROM python:3.12-slim

COPY docker/lambda-adapter /opt/extensions/lambda-adapter
RUN chmod 755 /opt/extensions/lambda-adapter

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    AWS_LWA_READINESS_CHECK_PATH=/api/ping/ \
    AWS_LWA_READINESS_CHECK_PORT=8000 \
    WEB_CONCURRENCY=1 \
    HOME=/tmp \
    TMPDIR=/tmp

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x entrypoint.sh

# WhiteNoise assets in the image. Lambda skips entrypoint startup tasks.
# Render still collectstatics in build.sh. Compose still collectstatics on boot.
# Placeholder values are build-only: with DEBUG=False, settings.py refuses the insecure
# SECRET_KEY default and requires ALLOWED_HOSTS and a non-local FRONTEND_URL. Real values
# come from the runtime environment.
RUN SECRET_KEY=collectstatic-build-placeholder-not-used-at-runtime \
    ALLOWED_HOSTS=build.invalid \
    FRONTEND_URL=https://build.invalid \
    DEBUG=False \
    EMAIL_PROVIDER=console \
    DATABASE_URL=sqlite:////tmp/build.db \
    python manage.py collectstatic --noinput

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]

# Timeout 45s is below Lambda's 60s so a hung worker dies before the
# invoke is cut. --worker-tmp-dir /tmp avoids Gunicorn writing HOME
# (/home/sbx_user1051 is read-only on Lambda).
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "1", "--timeout", "45", "--worker-tmp-dir", "/tmp"]
