# AWS Lambda Web Adapter (multi-arch). Documented image:
# public.ecr.aws/awsguru/aws-lambda-adapter:1.1.0
# Local Compose does not invoke the extension; Lambda's runtime does
# and proxies HTTP to PORT.
FROM public.ecr.aws/awsguru/aws-lambda-adapter:1.1.0 AS adapter

FROM python:3.12-slim

COPY --from=adapter /lambda-adapter /opt/extensions/lambda-adapter

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    AWS_LWA_READINESS_CHECK_PATH=/api/ping/ \
    AWS_LWA_READINESS_CHECK_PORT=8000 \
    WEB_CONCURRENCY=1

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

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "1", "--timeout", "120"]
