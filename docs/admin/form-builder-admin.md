# Admin: Building & Managing Forms

Read [../features/dynamic-form-builder.md](../features/dynamic-form-builder.md) first for the concept — this page is the step-by-step guide.

## Creating a New Form

1. Go to **Admin → Forms → New Form**.
2. Give it a title and URL slug (e.g. "IconCoders 2026 Registration" → `iconcoders-2026-registration`).
3. Drag in fields from the palette: Text, Email, Phone, Number, Dropdown, Radio Button, Checkbox, Date, Time, File Upload, Multi File Upload, Paragraph, URL, Section, Conditional Logic.
4. For each field, set: label, whether it's required, validation (e.g. email format, max file size), and order.
5. Use **Conditional Logic** to show/hide a field based on a previous answer (e.g. "if Track = Hardware, show 'Component List' field").
6. Preview the form as an end user would see it.
7. Attach it to the relevant module item (e.g. link it as this Event's registration form).
8. Set the open/close schedule (see [Scheduling](../features/scheduling.md)) or publish immediately.

## Editing a Live Form
You can edit a form after it's published, but be careful: changing/removing a field that already has responses can make old response data harder to interpret. Prefer adding new optional fields over removing existing ones once responses have started coming in.

## Viewing & Managing Responses
- **Admin → Forms → [form] → Responses** shows every submission in a table.
- Filter/search by any field.
- **Export** to CSV/Excel — see [Data Export](../features/data-export.md).
- **Email respondents** — see [Custom Email & Notifications](../features/email-notifications.md).

## Closing a Form
Set a `close_at` date/time (auto-closes — see [Scheduling](../features/scheduling.md)), or close it manually from the form's admin page. A closed form still shows results/responses to Admins but stops accepting new submissions, and the public page shows "Registration closed."

## Related Docs
- [../features/dynamic-form-builder.md](../features/dynamic-form-builder.md)
- [../architecture/data-model-dynamic-forms.md](../architecture/data-model-dynamic-forms.md) — why this is a no-code, no-new-table process
- [module-management-feature-flags.md](module-management-feature-flags.md)
