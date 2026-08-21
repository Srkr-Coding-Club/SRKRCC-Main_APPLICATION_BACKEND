# Forms & Club Data Management Center Module

## Overview
The Forms & Data Management module powers dynamic registration forms, complex multi-field validations, response analytics, CSV bulk-ingestion pipelines, and member participation records for the SRKR Coding Club platform.

---

## Data Models

### 1. `Form`
- **Fields**: `title`, `slug`, `description`, `image_url`, `category`, `status` (`DRAFT`, `PUBLISHED`, `CLOSED`), `version`, `allow_multiple_responses`, `allow_response_editing`, `enable_prefill`, `allow_edits_until`, `open_at`, `close_at`.
- **Policy Controls**:
  - `allow_multiple_responses` (bool): When `False` (default for registrations), students can only submit once. If a student returns, they enter response review/edit mode.
  - `allow_response_editing` (bool): When `True`, students who previously submitted can update their answers.
  - `enable_prefill` (bool): When `True` (and `allow_multiple_responses=False`), the form submission engine automatically matches and pre-fills student profile details (Full Name, Email, Phone, Roll Number, Branch, Year, GitHub, LinkedIn).

### 2. `FormField`
- **Fields**: `form` (FK), `label`, `type` (TEXT, EMAIL, NUMBER, DROPDOWN, RADIO, CHECKBOX, MATRIX_*, SIGNATURE, RATING, etc.), `placeholder`, `is_required`, `options`, `rows`, `min_value`, `max_value`, `conditional_logic`, `validation_rules`, `order`, `is_deleted`.

### 3. `Response` & `Answer`
- **Response**: `form` (FK), `user` (FK, nullable), `form_version`, `is_test_submission`, `is_manual_entry`, `created_by_admin` (FK), `submitted_at`.
- **Answer**: `response` (FK), `field` (FK), `value` (`JSONField`).

### 4. `BulkIngestSession`
- Tracks CSV bulk-ingestion runs with idempotency protection.
- **Fields**: `form` (FK), `idempotency_key` (unique), `created_by` (FK), `imported_count`, `skipped_count`, `duplicate_count`, `error_log` (`JSONField`), `status` (`COMPLETED`, `PARTIAL`, `FAILED`), `created_at`.

### 5. `MemberNote`
- Internal administrative notes attached to member profiles.
- **Fields**: `user` (FK), `note` (`TextField`), `created_by` (FK), `created_at`.

---

## API Endpoints

| Endpoint | Method | RBAC | Description |
| :--- | :--- | :--- | :--- |
| `/api/forms/` | GET, POST | Auth/Read-Only | List and create forms (annotated with `response_count`). |
| `/api/forms/{slug}/` | GET, PUT, PATCH, DELETE | Auth/Read-Only | Retrieve/update form schema and increment version. |
| `/api/forms/{slug}/bulk-ingest/` | POST | Staff / Admin | Ingest CSV rows with idempotency, visible field evaluation, and per-row savepoints. |
| `/api/forms/{slug}/check-duplicates/` | POST | Staff / Admin | Query existing email responses using JSONField-safe `Cast` lookup. |
| `/api/forms/data-health/` | GET | Staff / Admin | Diagnostic statistics, active system warnings, and recent activity log. |
| `/api/forms/{slug}/responses/` | GET | Staff / Admin | Paginated response viewer with search, date range, and flag filters. |
| `/api/members/` | GET | Staff / Admin | Aggregated member submissions directory with search and form filtering. |

---

## Architecture & Data Integrity Guarantees

1. **Per-Row Transactional Savepoints**: In `bulk_ingest`, each row is wrapped in `transaction.atomic()`, ensuring `skip_errors=True` commits all valid rows while logging bad rows without rolling back the entire batch.
2. **Idempotency Control**: Ingestion requests verify `idempotency_key` against `BulkIngestSession`. Repeated requests return cached summaries without duplicate writes.
3. **JSONField String Matching**: Duplicate email checks annotate `Answer.value` with `Cast('value', output_field=TextField())` and match against JSON-encoded email strings (`"email@domain.com"`).
4. **Anonymous-Safe Aggregation**: `MemberViewSet` explicitly filters out `user__isnull=True` prior to `values('user').annotate(...)` to prevent grouping anonymous manual entries under null.
