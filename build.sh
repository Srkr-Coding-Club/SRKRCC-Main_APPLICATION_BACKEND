#!/usr/bin/env bash
# Exit on error
set -o errexit

echo "========================================"
echo "SRKR Coding Club Backend — Build Process"
echo "========================================"

# 1. Install dependencies
echo "--> Installing dependencies..."
python -m pip install --upgrade pip
pip install -r requirements.txt

# 2. Database Migrations
echo "--> Running database migrations..."
python manage.py migrate --noinput

# 3. Collect Static Files for WhiteNoise
echo "--> Collecting static files..."
python manage.py collectstatic --noinput

# 4. Idempotently Seed Initial Feature Flags
echo "--> Seeding default feature flags..."
python manage.py seed_flags

# 5. Idempotently Create Super Admin if needed
echo "--> Ensuring Super Admin user is provisioned..."
python manage.py setup_admin

echo "========================================"
echo "Build finished successfully!"
echo "========================================"
