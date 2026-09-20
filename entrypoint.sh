#!/usr/bin/env bash
set -e

echo "--> Applying database migrations..."
python manage.py migrate --noinput

echo "--> Collecting static files..."
python manage.py collectstatic --noinput

echo "--> Seeding default feature flags..."
python manage.py seed_flags

echo "--> Setting up super admin user..."
python manage.py setup_admin

echo "--> Starting backend process..."
exec "$@"
