# SRKR Coding Club — Main Application (Backend)

> **One Platform. Many Features. Limitless Possibilities.**  
> The official unified backend API for SRKR Coding Club, powering events, hackathons (IconCoders), daily coding problems (Codequest), career drives, blogs, dynamic registration forms, and role-based access control.

---

## 📌 Project Overview

The **SRKR Coding Club Backend** is built using **Django 5.x** and **Django REST Framework (DRF)**, backed by **PostgreSQL** and **Redis + Celery**. 

Key technical highlights:
* **Dynamic Form Builder Engine**: Generates dynamic registration forms without needing new database migrations or tables per event/hackathon.
* **Two-Layer Visibility System**: Features module-level feature flags (on/off toggles) and scheduled date-window visibility (`visible_from` / `visible_until`).
* **Role-Based Access Control (RBAC)**: Hierarchical roles (`MEMBER`, `VOLUNTEER`, `JUDGE`, `CLUB_LEAD`, `ADMIN`) with scoped permissions.
* **Fast Developer Setup**: Uses `uv` for lightning-fast virtual environment creation and package installation.

---

## 🌿 Branching Strategy & Git Workflow

We maintain a strict 3-tier branching strategy across our repositories:

```
feature/*  ──────► dev (Integration) ──────► staging (QA/Testing) ──────► main (Production)
```

* **`main` (Production)**: Live production release branch. Only merges from `staging`.
* **`staging` (QA / Pre-Production)**: Testing and release candidate staging branch.
* **`dev` (Development Integration)**: Active integration branch. **All PRs target `dev`**.

> See **[CONTRIBUTING.md](CONTRIBUTING.md)** for our complete pull request protocol, branch conventions, and git cheat sheet.

---

## ⚙️ Prerequisites

Before setting up the backend, ensure you have the following installed on your machine:

| Requirement | Minimum Version | Recommendation / Notes |
|---|---|---|
| **Python** | `3.10.x` or `3.12.x` | [Download Python](https://www.python.org/downloads/) |
| **uv** | `0.1.0+` | Fast Python package manager (`pip install uv` or `choco install uv`) |
| **PostgreSQL** | `14.0+` | Local PostgreSQL instance / pgAdmin installed |
| **Redis** | `6.0+` | *(Optional during dev)* Required for Celery background tasks |
| **GNU Make** | Any | *(Optional)* Included in Windows Git / Chocolatey for fast `make` commands |

---

## 🚀 Complete Quick Start Guide

### 1. Database Setup (PostgreSQL)

Ensure your local PostgreSQL service is running (e.g., via pgAdmin or PostgreSQL service).

Create a PostgreSQL database named `srkrcc_db` (or let our automated setup script handle it in step 3):
- **Database Name**: `srkrcc_db`
- **Default Username**: `postgres`
- **Default Password**: `postgres`
- **Port**: `5432`

---

### 2. Environment Configuration (`.env`)

Create a `.env` file in the root of `SRKRCC-Main_APPLICATION_BACKEND` (or copy from `.env.example`):

```env
DEBUG=True
SECRET_KEY=django-insecure-srkrcc-dev-secret-key-change-in-prod
ALLOWED_HOSTS=localhost,127.0.0.1
CORS_ALLOWED_ORIGINS=http://localhost:3000

# PostgreSQL Connection String (adjust user:password@host:port if needed)
DATABASE_URL=postgres://postgres:postgres@localhost:5432/srkrcc_db

# Redis & Celery
REDIS_URL=redis://localhost:6379/0
CELERY_BROKER_URL=redis://localhost:6379/0
```

---

### 3. Automated One-Command Setup

Run the setup command from the backend folder:

```bash
make setup
```

*What `make setup` does automatically:*
1. Creates the Python virtual environment at `venv/` using `uv`.
2. Installs all required packages from `requirements.txt`.

---

### 4. Database Migrations & Initial Seeding

Run the following commands to initialize PostgreSQL tables and seed default feature flags:

```bash
# Auto-create srkrcc_db if not created yet (Optional helper)
python scripts/create_db.py

# Apply database migrations
make migrate

# Seed default module feature flags (Events, Hackathons, Codequest, etc.)
python scripts/seed_flags.py

# Create Django Admin superuser (Follow prompt for username/password)
make superuser
```

---

### 5. Running the Backend Server

Start the Django REST API development server on `http://localhost:8000`:

```bash
make dev
# Or directly via venv:
venv\Scripts\python manage.py runserver 8000
```

* **Admin Panel**: Visit `http://localhost:8000/admin/` and log in with your superuser credentials.
* **API Endpoints**: Visit `http://localhost:8000/api/feature-flags/` or `http://localhost:8000/api/events/`.

---

## 🛠️ Developer Commands Cheat Sheet

| Task | Command | Description |
|---|---|---|
| **Environment Setup** | `make setup` | Creates `venv` and installs dependencies via `uv`. |
| **Run Dev Server** | `make dev` | Starts server on `http://localhost:8000`. |
| **Create Migrations** | `make migrations` | Generates schema migration files for model changes. |
| **Apply Migrations** | `make migrate` | Applies pending migrations to local PostgreSQL. |
| **Create Superuser** | `make superuser` | Interactive prompt to create an Admin user. |
| **Django System Check** | `make check` | Runs Django system verification check. |
| **Interactive Shell** | `make shell` | Opens Django Python shell. |

---

## 📂 Repository Directory Structure

```
SRKRCC-Main_APPLICATION_BACKEND/
├── AGENTS.md                  ← AI Agent guidelines, SOLID rules, & doc protocol
├── CONTRIBUTING.md            ← Branching strategy, PR guide, & git cheat sheet
├── Makefile                   ← Developer shortcuts for uv, migrations, and server
├── requirements.txt           ← Python dependency manifest
├── manage.py                  ← Django CLI manager
├── config/                    ← Django core configurations
│   ├── settings.py            ← DRF, PostgreSQL, CORS, Celery settings
│   ├── urls.py                ← Main API route definitions
│   ├── wsgi.py / asgi.py      ← Server entrypoints
│   └── celery.py              ← Celery task runner setup
├── apps/                      ← Modular Bounded Context Applications
│   ├── core/                  ← Base models (TimeStampedModel)
│   ├── accounts/              ← User model, RBAC Roles, SimpleJWT Auth
│   ├── feature_flags/          ← Module toggle engine
│   ├── forms/                 ← Dynamic Form Builder (Forms, Fields, Responses)
│   ├── events/                ← Workshops & meetups module
│   ├── hackathons/            ← Hackathons & IconCoders engine
│   ├── codequest/             ← Daily Problem of the Day & streak tracker
│   ├── career/                ← Job/internship listings
│   ├── blogs/                 ← Technical articles & tutorials
│   └── audit/                 ← Admin audit logging system
├── docs/                      ← Architectural & technical learning guides
│   ├── README.md              ← Documentation home
│   └── architecture/          ← In-depth technical learning guides
└── scripts/                   ← Automation scripts (create_db.py, seed_flags.py)
```

---

## 📚 Technical Documentation & Learning Guides

For under-the-hood technical details on how Django, DRF, PostgreSQL, Celery, and the Dynamic Form Builder work:
* **[AGENTS.md](AGENTS.md)** — Architectural preservation & agent rules
* **[CONTRIBUTING.md](CONTRIBUTING.md)** — Branching strategy & Git contribution guide
* **[docs/architecture/technical-learning-guide.md](docs/architecture/technical-learning-guide.md)** — Deep-dive technical learning guide
* **[docs/architecture/data-model-dynamic-forms.md](docs/architecture/data-model-dynamic-forms.md)** — Dynamic Form Builder metadata model