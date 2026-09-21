# Tech Stack

The exact technologies used to build and run the platform, and why each was picked.

| Area | Technology | Notes |
|---|---|---|
| **Frontend** | Next.js 15, React, Tailwind CSS, Shadcn UI | Server-rendered pages for speed and SEO (important for public pages like Blog, Events, Career). |
| **Backend** | Django + Django REST Framework (DRF) | Handles auth, business logic, and exposes APIs the frontend calls. |
| **Database** | PostgreSQL | Stores users, forms, responses, events, everything structured. |
| **Cache** | Django's default in-process cache (`LocMemCache`) | Backs the password-setup-link rate limiter (`apps/accounts/services/password_setup_service.py`). Not Redis-backed — see note below. |
| **Background jobs** | Plain Python `threading.Thread` (`apps/core/tasks.py::run_in_background`) | Runs bulk email sends and large DMC exports off the request/response cycle. Not a task queue — no broker, no retry, no cross-process persistence. See note below. |
| **File Storage** | Cloudflare R2 (S3-compatible) | Stores images, PDFs, resumes, posters. Served via CDN for fast loading. |
| **Authentication** | Django AllAuth, JWT | Handles login/signup and issues tokens the frontend uses to call the API securely. |
| **Email Service** | Resend / Brevo | Sends transactional emails (confirmations, reminders, results) and bulk notifications. |
| **Analytics (optional)** | PostHog | Tracks page views and engagement, feeds the Analytics & Insights dashboards. |

## How a request flows through the stack

1. A member visits a page (e.g. `/hackathons/iconcoders`) → served by **Next.js**.
2. The page calls the **Django REST API** to fetch data (e.g. hackathon details, registration form).
3. Django checks **Role Based Access** rules, reads/writes **PostgreSQL**, and reads files from **Cloudflare R2** if needed.
4. If an action needs to happen off the request (e.g. "email everyone who registered"), the view starts a **background thread** and returns immediately — see the note below. "Auto-hide this event page after its end date" isn't a live background job at all: the next read of that item re-checks its timestamps and flips its status if needed (see [../features/scheduling.md](../features/scheduling.md)).
5. Emails go out through Django's `EmailMultiAlternatives` (console backend in dev; SMTP in production — Resend/Brevo integration is planned, not yet wired up); analytics events are captured by **PostHog** (if enabled).

## Background Jobs & Caching (current state)

Earlier versions of these docs described Redis + Celery as already running. They aren't:

- **No Redis instance and no Celery worker/beat process are deployed** — `render.yaml` (the actual deployment config) defines only the single Django web service, and `requirements.txt` doesn't include either package. `REDIS_URL`/`CELERY_BROKER_URL` still exist in `.env.example` as a documented future option, not as something the running app reads.
- **"Background jobs" today = `apps/core/tasks.py::run_in_background`**, a plain daemon `threading.Thread`. Every job it runs (bulk email sends, DMC exports over the sync-export row threshold) first writes a durable status row (`EmailJob`, `ExportJob`) *before* dispatching, so the job's last-known status survives even if the thread dies — the tradeoff is that in-flight work itself doesn't survive a process restart, and there's no automatic retry. See the docstring in that file for the full reasoning.
- **"Caching" today = Django's default `LocMemCache`**, used only for the password-setup-link rate limiter. There is no dashboard/query result caching layer.
- This is a deliberate, documented tradeoff for the app's current college-club scale, not an oversight — revisit if job volume or read load ever genuinely need a real queue/cache.

## Related Docs
- [README.md](README.md) — architecture overview
- [deployment-infra.md](deployment-infra.md) — where each of these runs
- [../features/scheduling.md](../features/scheduling.md) — how scheduled publish/hide actually works (lazy sync on read, not a Celery beat)
