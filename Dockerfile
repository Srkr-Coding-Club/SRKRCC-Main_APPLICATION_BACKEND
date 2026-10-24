# Lambda Web Adapter 1.1.0. GitHub runners cannot pull the public ECR image
# (anonymous 429). The adapter's Cargo config uses musl-gcc; -static keeps
# the binary from needing musl's dynamic loader, which Lambda does not have.
# Without that, the function URL returns 502. Local Compose does not invoke
# the extension.
FROM rust:1-bookworm AS adapter
RUN apt-get update && apt-get install -y --no-install-recommends musl-tools \
    && rm -rf /var/lib/apt/lists/* \
    && rustup target add x86_64-unknown-linux-musl
WORKDIR /src
ADD https://github.com/aws/aws-lambda-web-adapter/archive/refs/tags/v1.1.0.tar.gz /tmp/adapter.tar.gz
RUN tar -xzf /tmp/adapter.tar.gz -C /src --strip-components=1 \
    && RUSTFLAGS="-C target-feature=+crt-static -C link-arg=-static" \
       cargo build --release --locked --target x86_64-unknown-linux-musl --bin lambda-adapter \
    && if ! ldd target/x86_64-unknown-linux-musl/release/lambda-adapter 2>&1 | grep -q 'not a dynamic executable'; then \
         echo "lambda-adapter must be statically linked for Lambda" >&2; \
         ldd target/x86_64-unknown-linux-musl/release/lambda-adapter; \
         exit 1; \
       fi

FROM python:3.12-slim

COPY --from=adapter /src/target/x86_64-unknown-linux-musl/release/lambda-adapter /opt/extensions/lambda-adapter

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
