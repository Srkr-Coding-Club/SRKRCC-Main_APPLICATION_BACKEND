# Data Management Center (DMC)

## Overview

The DMC is a **metadata-driven admin workspace** that exposes a unified, permission-aware view over all application data entities — Users, Form Responses, Hackathon Teams/Participants/Submissions, Event Registrations, CodeQuest Submissions, Career Applications, and Job Listings.

The architecture follows a strict principle: **DMC is a generic data engine sitting above the existing application data — not a collection of custom admin tables.**

---

## Architecture

```
Frontend (Next.js)
  └── DataManagementCenter.tsx
       ├── useDMCCatalog  → GET /api/admin/dmc/datasets/
       ├── useDMCSchema   → GET /api/admin/dmc/datasets/<id>/schema/
       ├── useDMCQuery    → POST /api/admin/dmc/datasets/<id>/query/
       ├── useDMCColumns  → client-side column toggle state
       └── dmcApi.export  → POST /api/admin/dmc/datasets/<id>/export/

Backend (Django DRF)
  └── apps/core/dmc/
       ├── contracts.py      → Dataclass contracts (source of truth)
       ├── permissions.py    → RBAC: dataset-level, column-level, export-level
       ├── registry.py       → Single registration point for all datasets
       ├── models.py         → ExportJob (async export tracking)
       ├── export_service.py → ExportService (CSV/XLSX/JSON serialization)
       ├── views.py          → 7 DRF API views
       ├── urls.py           → /api/admin/dmc/ routes
       └── adapters/
            ├── base.py          → BaseDatasetAdapter (abstract)
            ├── users.py         → Users & Members
            ├── forms.py         → FormsAll + FormIndividual (2-step pagination)
            ├── hackathons.py    → Participants, Teams, Submissions
            └── events_codequest_careers.py → Events, CodeQuest, Careers
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/admin/dmc/datasets/` | List all accessible datasets |
| GET | `/api/admin/dmc/datasets/<id>/schema/` | Column + filter definitions |
| POST | `/api/admin/dmc/datasets/<id>/query/` | Paginated filtered query |
| GET | `/api/admin/dmc/datasets/<id>/records/<pk>/` | Single record detail |
| POST | `/api/admin/dmc/datasets/<id>/export/` | Export (sync ≤1000 rows, async >1000) |
| GET | `/api/admin/dmc/exports/<job_id>/` | Export job status |
| GET | `/api/admin/dmc/exports/<job_id>/download/` | Download completed export |

---

## Permission Model

| Role | Datasets accessible |
|------|-------------------|
| Superuser / Staff | All datasets |
| ADMIN | All datasets |
| CLUB_LEAD | All except `users`, `career_applications` |

Sensitive columns (`password`, `token`, `secret`, etc.) are **always stripped** via `FORBIDDEN_COLUMN_KEYS` regardless of role.

---

## Adding a New Dataset

1. Create an adapter in `apps/core/dmc/adapters/<name>.py` implementing `BaseDatasetAdapter`.
2. Register it in `apps/core/dmc/registry.py` inside `_build_registry()`.
3. Nothing else changes. The frontend discovers it automatically via `/datasets/`.

---

## Four-State Canonical Value Model

Every field value is normalized to one of four states:

| State | Meaning | Display |
|-------|---------|---------|
| `value` | Valid data | Rendered via type-specific formatter |
| `not_applicable` | Field doesn't exist for this dataset | `—` |
| `empty` | Field exists but blank | `(empty)` |
| `unknown` | Expected data could not be resolved | `[Unavailable]` |

---

## Row Selection & Bulk Email

The grid supports per-row and select-all-on-page checkboxes
(`DataManagementCenter.tsx`'s `DataGrid`), keyed by each record's `id` column
(always present in query results regardless of which columns are toggled
visible — `id` isn't in `FORBIDDEN_COLUMN_KEYS`). Selection is deliberately
**scoped to the currently-loaded page**: it's cleared on search/sort/filter/page
changes rather than tracked across pages, since the "Email Selected" action reads
name/email straight out of the loaded `records` — this also keeps recipient counts
bounded (no "select all matching filter" spanning thousands of rows).

Two actions consume the selection:

- **Export → "Selected Rows Only"** — passes the selected IDs as
  `selected_record_ids` to `POST /datasets/<id>/export/` (previously dead: the
  option existed in the UI but always sent an empty array).
- **"Email Selected (n)"** — shown when the active dataset has a column of type
  `email`. Opens `EmailTemplateEditor` (`mode="send"`), which either picks an
  existing [Email Template](../features/email-notifications.md) or drafts one
  inline, then dispatches to the selected rows' emails via the same
  `POST /api/auth/emails/send/` endpoint the admin email system already uses —
  no DMC-specific send path.

## Export Strategy

- **Synchronous** (≤ `DMC_SYNC_EXPORT_MAX_ROWS` = 1000 rows): Returns file inline as HTTP response.
- **Asynchronous** (> 1000 rows): Creates `ExportJob`, dispatches a plain background thread via `apps/core/tasks.py::run_in_background` (`apps/core/dmc/tasks.py` — **not Celery**, no task queue in this app), returns `job_id` for polling.
- Adapters provide `stream_records()` generator — ExportService handles CSV/XLSX/JSON serialization.

---

## Key Invariants

1. **1 row = 1 domain entity** — never a JOIN that multiplies rows.
2. **Adapters never format CSV/XLSX/JSON** — ExportService owns all serialization.
3. **Sort field allowlisted** per dataset — prevents arbitrary ORDER BY injection.
4. **Column permissions enforced server-side** before any data reaches the client.
