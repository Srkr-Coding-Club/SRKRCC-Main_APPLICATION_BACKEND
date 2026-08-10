# Tech Stack

The exact technologies used to build and run the platform, and why each was picked.

| Area | Technology | Notes |
|---|---|---|
| **Frontend** | Next.js 15, React, Tailwind CSS, Shadcn UI | Server-rendered pages for speed and SEO (important for public pages like Blog, Events, Career). |
| **Backend** | Django + Django REST Framework (DRF) | Handles auth, business logic, and exposes APIs the frontend calls. |
| **Database** | PostgreSQL | Stores users, forms, responses, events, everything structured. |
| **Cache & Queue** | Redis, Celery | Redis caches frequently-read data (e.g. dashboards); Celery runs background jobs (sending bulk emails, scheduled publish/unpublish of forms and events). |
| **File Storage** | Cloudflare R2 (S3-compatible) | Stores images, PDFs, resumes, posters. Served via CDN for fast loading. |
| **Authentication** | Django AllAuth, JWT | Handles login/signup and issues tokens the frontend uses to call the API securely. |
| **Email Service** | Resend / Brevo | Sends transactional emails (confirmations, reminders, results) and bulk notifications. |
| **Analytics (optional)** | PostHog | Tracks page views and engagement, feeds the Analytics & Insights dashboards. |

## How a request flows through the stack

1. A member visits a page (e.g. `/hackathons/iconcoders`) → served by **Next.js**.
2. The page calls the **Django REST API** to fetch data (e.g. hackathon details, registration form).
3. Django checks **Role Based Access** rules, reads/writes **PostgreSQL**, and reads files from **Cloudflare R2** if needed.
4. If an action needs to happen later or asynchronously (e.g. "email everyone who registered," or "auto-hide this event page after its end date"), it's queued in **Celery** via **Redis**.
5. Emails go out through **Resend/Brevo**; analytics events are captured by **PostHog** (if enabled).

## Related Docs
- [README.md](README.md) — architecture overview
- [deployment-infra.md](deployment-infra.md) — where each of these runs
- [../features/scheduling.md](../features/scheduling.md) — how Celery powers scheduled publish/hide
