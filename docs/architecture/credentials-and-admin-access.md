# Django Admin & Authentication Credentials Guide

This guide documents **HTTP client architecture (Fetch vs. Axios)**, **Django Admin portal activation**, **RBAC authorization rules**, and **pre-configured database credentials & seeders** across the SRKRCC platform.

---

## 1. HTTP Client Architecture: Fetch vs. Axios

### Why the Frontend Uses Native `fetch` with `fetchApi`
- **Next.js 15 App Router Compatibility**: React Server Components (RSC) and Next.js Edge Middleware natively intercept and optimize global `fetch()` calls for streaming, caching, and Server-Side Rendering.
- **Lightweight & Zero-Bundle Overhead**: Native `fetch()` requires zero additional client bundle size.
- **Custom `fetchApi` Wrapper (`src/lib/api-client.ts`)**:
  - Sets `credentials: 'include'` to automatically forward secure `HttpOnly` JWT session cookies.
  - Automatically attaches `Authorization: Bearer <access_token>` when token-based communication is used.
  - Transparently intercepts `401 Unauthorized` responses and executes automatic token rotation via `POST /api/auth/refresh`.
  - Implements connection timeouts (`AbortController`) with graceful offline fallbacks.

> [!NOTE]
> `axios` is present in `package.json` as an optional dependency, but all core application and administrative data pipelines use the standardized, type-safe `fetchApi` utility in [src/lib/api-client.ts](file:///c:/Users/chall/OneDrive/Desktop/SRKRCC-Main_APPLICATION_FRONTEND/src/lib/api-client.ts).

---

## 2. Django Admin Feature & Portal

The Django Admin is active on the backend at:
**`http://localhost:8000/admin/`**

### Active Registered Admin Models
| App Name | Models Registered in Django Admin |
| :--- | :--- |
| **Accounts** | `User` (with role, branch, year, roll number fieldsets) |
| **Forms** | `Form`, `FormField` (inline), `Response`, `Answer` (inline), `BulkIngestSession`, `MemberNote` |
| **Events** | `Event` (category, venue, timestamps, capacity, registration_form) |
| **Hackathons** | `Hackathon`, `Team`, `Submission` |
| **CodeQuest** | `Problem`, `Submission`, `UserStreak` |
| **Career** | `JobListing`, `JobApplication` |
| **Blogs** | `BlogPost` |
| **Feature Flags** | `FeatureFlag` |
| **Audit** | `AuditLog` |

---

## 3. Pre-Configured Credentials

The database comes provisioned with the following test and administrative credentials:

| Portal | Email | Password | Role | Permissions & Clearances |
| :--- | :--- | :--- | :--- | :--- |
| **Django Admin & Frontend** | `admin@srkr.ac.in` | `Admin@123` | `ADMIN` | **Superuser & Staff** — Full backend & frontend control |
| **Django Admin & Frontend** | `clublead@srkr.ac.in` | `ClubLead@123` | `CLUB_LEAD` | **Staff** — Full frontend admin control room & data health |
| **Frontend Portal** | `judge@srkr.ac.in` | `Judge@123` | `JUDGE` | Hackathon project evaluation & grading |
| **Frontend Portal** | `rahul.sharma@srkr.ac.in` | `Member@123` | `MEMBER` | Pre-seeded with 14-day CodeQuest streak & form submissions |
| **Frontend Portal** | `member@srkr.ac.in` | `Member@123` | `MEMBER` | Event registration, CodeQuest, member profile |

---

## 4. Master Database Seeding Commands

### A. Master Full Platform Seeder (Recommended)
Seeds core users, live dynamic forms, user response data, CodeQuest problems, active 14-day streaks, feature flags, and audit logs:

```powershell
# Using Make
make seed-db

# Or directly with Python
python scripts/seed_full_database.py
```

### B. User Credentials Only Seeder
Seeds and resets default passwords for the 4 core platform roles:

```powershell
make seed-users
# Or: python scripts/seed_default_users.py
```

---

## 5. Centralized Role-Based Access Control (RBAC)

RBAC permission enforcement is centralized in [apps/core/permissions.py](file:///c:/Users/chall/OneDrive/Desktop/SRKRCC-Main_APPLICATION_BACKEND/apps/core/permissions.py):

| DRF Permission Class | Target Roles | Protected Endpoints |
| :--- | :--- | :--- |
| **`IsAdminOrClubLead`** | `ADMIN`, `CLUB_LEAD`, Staff, Superusers | `/api/audit/`, `/api/members/`, `/api/forms/data-health/`, `/api/forms/summary-stats/`, `/api/forms/{slug}/bulk-ingest/` |
| **`IsJudgeOrAdmin`** | `ADMIN`, `CLUB_LEAD`, `JUDGE` | `/api/hackathons/evaluations/` |
| **`IsClubMember`** | Authenticated Users | `/api/forms/submissions/`, `/api/auth/me/`, `/api/codequest/submissions/` |
| **`IsAuthenticatedOrReadOnly`** | Public / Authenticated | `/api/forms/`, `/api/events/`, `/api/feature-flags/` |

---

## 6. How to Create a Custom Django Superuser

To provision a custom superuser interactively:

```powershell
cd c:\Users\chall\OneDrive\Desktop\SRKRCC-Main_APPLICATION_BACKEND
.\venv\Scripts\Activate.ps1
python manage.py createsuperuser
```
