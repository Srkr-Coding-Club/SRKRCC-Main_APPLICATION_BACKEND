# Data Model: How Dynamic Forms Are Stored

This explains the technical design behind the [Dynamic Form Builder](../features/dynamic-form-builder.md) feature — why the platform never needs a new database table for a new form.

## The problem with the "traditional" approach

Most simple systems create a brand-new database table every time someone needs a new form:

```
Hackathon Registration → Table 1
Volunteer Registration → Table 2
CodeFest Registration  → Table 3
...                     → N Tables
```

This breaks down fast:
- Hundreds of tables pile up over a few years of club activity.
- Hard to maintain — every form change needs a database migration.
- Migrations become risky and slow ("nightmare").
- Difficult to build unified analytics across forms (each table looks different).

## Our approach: one flexible structure for unlimited forms

Instead of a table per form, the platform stores forms as **metadata** — data that describes data:

```mermaid
erDiagram
    FORMS ||--o{ FORM_FIELDS : has
    FORMS ||--o{ RESPONSES : receives
    FORM_FIELDS ||--o{ ANSWERS : "answered in"
    RESPONSES ||--o{ ANSWERS : contains

    FORMS {
        id id
        string title
        string slug
        string status
        bool club_id_enabled
        string club_id_prefix
        json club_id_field_mapping
        bool confirmation_email_enabled
        id confirmation_email_template
    }
    FORM_FIELDS {
        id id
        id form_id
        string label
        string type
        json rules
    }
    RESPONSES {
        id id
        id form_id
        id user_id
        datetime submitted_at
    }
    ANSWERS {
        id id
        id response_id
        id field_id
        json value
    }
}
```

- **Forms** — one row per form (its title, URL slug, whether it's published/draft/closed).
- **Form Fields** — one row per question in a form (label, type, validation rules, order).
- **Responses** — one row every time someone submits a form.
- **Answers** — one row per answer to a specific field within a response.

A new registration form (say, for a brand-new hackathon) is just new rows in `Forms` and `Form Fields` — **no schema change, no migration, no developer needed.**

## Supported field types

Text · Email · Phone · Number · Dropdown · Radio Button · Checkbox · Date · Time · File Upload · Multi File Upload · Paragraph · URL · Section · Conditional Logic

"Conditional Logic" lets a field appear only if a previous answer matches a condition (e.g. "If team size > 1, show teammate details").

## Submission-time automation (Club ID + confirmation email)

A `Form` can optionally carry two automations, both configured entirely through
metadata — no code or migration per form, same principle as the rest of this design:

- `club_id_enabled` / `club_id_prefix` / `club_id_field_mapping` — when enabled,
  `club_id_field_mapping` points at this form's own `FormField` IDs (e.g.
  `{"email": 42, "full_name": 43}`). On each completed submission,
  `apps/forms/services.py::FormAutomationService.resolve_club_member` reads the
  submitted `Answer` for the mapped email field and calls
  `UserAccountService.upsert_member` — the same find-or-create-by-email operation
  the CSV member-import pipeline already uses. A new email gets a fresh, permanent
  Club ID (`ClubIDService.allocate_next_club_id`, race-safe via a row-locked
  sequence counter); an email that already has one keeps it. This is why the `User`
  table doubles as the club member directory rather than a separate table existing
  alongside it — one source of truth for "does this email already have a Club ID."
- `confirmation_email_enabled` / `confirmation_email_template` — a `ForeignKey` to
  `core.EmailTemplate`. When set, the submitter receives that template (rendered
  with their own submitted details) once the response is fully saved. See
  [../features/email-notifications.md](../features/email-notifications.md).

Both run inside `ResponseViewSet.create()` (`apps/forms/views.py`): Club ID
resolution happens **inside** the same DB transaction as the response save (a
conflict rolls the whole submission back, so a Club ID is only ever persisted once
the response itself is fully persisted); the confirmation email dispatches **after**
that transaction commits, so a slow or failed send can never affect an otherwise
successful submission.

## Benefits

- **No new tables** — forms are added/edited entirely through the admin UI.
- **Easy to manage** — one place (Admin → Forms) to see and edit every form the club has ever run.
- **Scalable** — adding the 500th form is exactly as cheap as adding the 1st.
- **Analytics-friendly** — because every form follows the same shape, dashboards and exports work uniformly across all of them. See [../features/analytics-insights.md](../features/analytics-insights.md) and [../features/data-export.md](../features/data-export.md).

## Related Docs
- [../features/dynamic-form-builder.md](../features/dynamic-form-builder.md) — the admin-facing feature this powers
- [../admin/form-builder-admin.md](../admin/form-builder-admin.md) — how admins actually build a form
