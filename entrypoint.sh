#!/usr/bin/env bash
set -e

# Render does not use this script (see render.yaml + build.sh).
# Local Compose sets RUN_STARTUP_TASKS=1 so migrate/collectstatic/seed still
# run on container boot. Lambda must leave this unset: cold starts must not
# migrate or write into the image filesystem.
if [ "${RUN_STARTUP_TASKS:-0}" = "1" ]; then
  echo "--> Applying database migrations..."
  python manage.py migrate --noinput

  echo "--> Collecting static files..."
  python manage.py collectstatic --noinput

  echo "--> Seeding default feature flags..."
  python manage.py seed_flags

  echo "--> Setting up super admin user..."
  python manage.py setup_admin
fi

echo "--> Starting backend process..."
exec "$@"
