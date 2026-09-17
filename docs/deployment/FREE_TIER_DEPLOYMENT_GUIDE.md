# SRKR Coding Club — 100% Free-Tier Production Deployment Guide

> **Zero Hosting Cost. Production Ready. Infinite Scalability.**  
> This guide outlines how to deploy the entire unified SRKR Coding Club platform for **$0 / month** using modern developer-friendly cloud platforms:
> - **Frontend**: [Vercel](https://vercel.com) (Global Edge CDN, Automatic SSL, Next.js optimized)
> - **Backend API**: [Render](https://render.com) (Free Python 3 Web Service with Gunicorn & WhiteNoise)
> - **PostgreSQL Database**: [Neon.tech](https://neon.tech) (Free Serverless PostgreSQL that never sleeps) or [Render Postgres](https://render.com)

---

## 🏗️ Architecture Overview

```
                          ┌──────────────────────────┐
                          │   Vercel Global Edge     │
                          │   (Next.js 15 Frontend)  │
                          │   https://srkrcc.vercel.app │
                          └─────────────┬────────────┘
                                        │
                         HTTPS REST / JWT Calls
                                        │
                                        ▼
                          ┌──────────────────────────┐
                          │    Render Web Service    │
                          │    (Django 5 + Gunicorn) │
                          │ https://srkrcc-api.onrender.com │
                          └─────────────┬────────────┘
                                        │
                             Encrypted SSL Connection
                                        │
                                        ▼
                          ┌──────────────────────────┐
                          │      Neon.tech / AWS     │
                          │ (PostgreSQL 15 Serverless│
                          └──────────────────────────┘
```

---

## 🚀 Step-by-Step Free Deployment

### Step 1: Create Free Serverless PostgreSQL on Neon.tech (2 Minutes)

We recommend **Neon.tech** for the database because its free tier **never suspends or sleeps**, provides **0.5 GB storage**, and works with Django out of the box.

1. Sign up for free at [neon.tech](https://neon.tech) (using GitHub or Google).
2. Click **Create Project**:
   - Name: `srkrcc-production-db`
   - Postgres version: `16` or `15`
   - Region: `AWS / Singapore` (or closest to India)
3. Under **Connection Details**, copy the **Connection String**:
   ```
   postgres://username:password@ep-xyz-123.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
   ```
   *(Keep this string handy — this will be your `DATABASE_URL`)*.

---

### Step 2: Deploy Backend to Render (5 Minutes)

1. Sign up or log into [render.com](https://render.com).
2. Click **New +** -> **Web Service**.
3. Connect your GitHub repository: `SRKRCC-Main_APPLICATION_BACKEND`.
4. Configure the Web Service settings:
   - **Name**: `srkrcc-backend`
   - **Region**: `Singapore` (select same region as database)
   - **Branch**: `main` (or your active release branch)
   - **Root Directory**: `.` (leave blank or `./`)
   - **Runtime**: `Python 3`
   - **Build Command**: `./build.sh`
   - **Start Command**: `gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 3 --timeout 120`
   - **Instance Type**: `Free` ($0/month)
5. Under **Environment Variables**, add:
   | Key | Value | Notes |
   |---|---|---|
   | `PYTHON_VERSION` | `3.12.0` | Ensures compatible Python runtime |
   | `DEBUG` | `False` | Production secure mode |
   | `SECRET_KEY` | *(Click Generate or paste a 50+ char random string)* | Required |
   | `DATABASE_URL` | *(Paste Neon connection string from Step 1)* | PostgreSQL DB |
   | `ALLOWED_HOSTS` | `.onrender.com,localhost,127.0.0.1` | Allows Render domain |
   | `CORS_ALLOW_ALL_ORIGINS` | `True` *(or specify frontend Vercel URL)* | Allows frontend calls |
   | `CSRF_TRUSTED_ORIGINS` | `https://*.onrender.com,https://*.vercel.app` | Prevents CSRF issues |
   | `DJANGO_SUPERUSER_EMAIL` | `admin@srkr.ac.in` | **Super Admin Email** |
   | `DJANGO_SUPERUSER_PASSWORD` | `YourSecurePassword@2025` | **Super Admin Password** |
   | `DJANGO_SUPERUSER_USERNAME` | `admin` | Super Admin Username |

6. Click **Deploy Web Service**!
   - What Render does automatically via `./build.sh`:
     1. Installs all packages (including `gunicorn` and `whitenoise`).
     2. Runs all database migrations (`migrate`).
     3. Collects static assets for the Django Admin (`collectstatic`).
     4. Seeds default feature flags (`seed_flags`).
     5. Automatically provisions your **Super Admin** with your specified password!
7. Once deployed, note down your backend URL (e.g., `https://srkrcc-backend.onrender.com`).

---

### Step 3: Deploy Frontend to Vercel (3 Minutes)

1. Sign up or log into [vercel.com](https://vercel.com) using GitHub.
2. Click **Add New...** -> **Project**.
3. Select `SRKRCC-Main_APPLICATION_FRONTEND`.
4. Configure the project:
   - **Framework Preset**: Next.js (Auto-detected)
   - **Root Directory**: `./`
5. Under **Environment Variables**, add:
   | Key | Value |
   |---|---|
   | `NEXT_PUBLIC_API_BASE_URL` | `https://srkrcc-backend.onrender.com/api` |
   *(Ensure to replace with your actual Render backend URL followed by `/api`)*.
6. Click **Deploy**.
   - Vercel will compile the Next.js App Router app across its edge network.
   - Your live site will be ready at: `https://srkrcc.vercel.app` (or your chosen project name).

---

### Step 4: Login as Super Admin & Verify Platform

1. Navigate to your live frontend URL:
   `https://srkrcc.vercel.app/login`
2. Enter your Super Admin credentials:
   - **Email**: `admin@srkr.ac.in` (or the email you set in `DJANGO_SUPERUSER_EMAIL`)
   - **Password**: The password you specified in `DJANGO_SUPERUSER_PASSWORD`
3. Access the **Admin Control Room**:
   - Feature flags management: `/admin/flags`
   - Dynamic form builder: `/admin/builder`
   - User roles & member management: `/admin/users`
   - QR code attendance scanner: `/admin/attendance/scan`
   - CSV data restoration & backup vault: `/admin/csv-ingestion`
4. Access the Django Admin interface directly:
   `https://srkrcc-backend.onrender.com/admin/`

---

## 💻 Local Testing & Development

### 1. Running in Development Mode
- **Backend**:
  ```bash
  cd SRKRCC-Main_APPLICATION_BACKEND
  .\venv\Scripts\python manage.py runserver
  ```
- **Frontend**:
  ```bash
  cd SRKRCC-Main_APPLICATION_FRONTEND
  pnpm run dev
  ```

### 2. Running Complete Production Stack with Docker Compose
You can test the entire production stack (PostgreSQL + Django Gunicorn + Next.js Standalone) locally on your machine with a single command:

```bash
cd SRKRCC-Main_APPLICATION_BACKEND
docker compose up --build
```
- Frontend will be live on `http://localhost:3000`
- Backend API will be live on `http://localhost:8000`
- PostgreSQL will be live on `localhost:5432`

### 3. Super Admin CLI Management Command
You can create or update superadmin accounts anytime via CLI:

```bash
# Create default admin:
python manage.py setup_admin

# Create custom admin with custom credentials:
python manage.py setup_admin --email president@srkr.ac.in --password "SuperSecret@2025" --username president --first-name "Club" --last-name "President"

# Reset existing admin password:
python manage.py setup_admin --email admin@srkr.ac.in --password "NewPassword@123" --reset-password
```

---

## 🔒 Security Best Practices Checklist for Production

- [x] `DEBUG` set to `False` in production.
- [x] `SECRET_KEY` set to a unique, random 50+ character string.
- [x] SSL redirect and secure cookies automatically activated when `DEBUG=False`.
- [x] WhiteNoise static file compression and caching enabled.
- [x] Admin passwords created via `set_password` with PBKDF2 SHA-256 hashing.
- [x] CORS origins restricted to your Vercel frontend domain in production.
- [x] CSRF trusted origins configured to avoid cross-origin submission failures.
