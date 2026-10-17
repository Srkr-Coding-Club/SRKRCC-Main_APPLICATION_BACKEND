# Module: Events

## Overview
The Events module covers everything that *isn't* a hackathon — workshops, seminars, guest talks, meetups, coding contests, orientation sessions. It's the general-purpose "something is happening, come register" engine for the club.

## Who It's For
Members who want to attend, and Club Leads/Admins who organize.

## Public Pages

| Page | Path (example) | What it shows | Visible to |
|---|---|---|---|
| Events listing | `/events` | All events (no server-side date filtering — `EventViewSet` returns `Event.objects.all()` unfiltered; see the Visibility Rules gap note below), filterable by category | Everyone |
| Event details | `/events/[slug]` | Description, date/time/venue (or online link), speaker info, registration button | Everyone |
| Registration form | `/events/[slug]/register` | Dynamic form (varies per event — could be as simple as name+email, or detailed) | Logged-in members (or public, if event is open) |
| My registrations | `/events/my` | List of events a member has registered for, with reminders | Logged-in members |

## Admin Pages

| Page | Path (example) | What it does | Who can access |
|---|---|---|---|
| Event manager | `/admin/events` | Create/edit events: title, date/time, venue, capacity, visibility window | Admin, Club Lead |
| Registration form builder | `/admin/events/[slug]/form` | Build the registration form via [Dynamic Form Builder](../features/dynamic-form-builder.md) | Admin, Club Lead |
| Attendee list | `/admin/events/[slug]/attendees` | View/export registrants, mark attendance, send reminders | Admin, Club Lead, Volunteer |

## Visibility Rules (Feature Flag + Event Dates)
1. **Module-level flag** can hide "Events" from the sidebar entirely — this part is real (see [Feature Flags](../features/feature-flags.md)).
2. **Per-event date window — not implemented.** `Event` does have `visible_from`/`visible_until` columns and they're editable via the API, but `EventViewSet.get_queryset()` (`apps/events/views.py`) is just `Event.objects.all()` — no date filtering at all. A past event does not automatically drop off `/events`; nothing currently archives it server-side. See [Scheduling](../features/scheduling.md) for the full per-item breakdown across modules.

```mermaid
flowchart LR
    A{Events module enabled?} -- No --> Z[Hidden entirely]
    A -- Yes --> B{Within event's\nvisible/registration window?}
    B -- Yes --> C[Shown on /events, registration open]
    B -- No, but event ended --> D[Moved to Past Events archive]
```

## User Flow

```mermaid
flowchart TD
    A[Browse /events] --> B[Open event details]
    B --> C[Click Register]
    C --> D[Fill dynamic form]
    D --> E[Confirmation email sent]
    E --> F[Reminder email before event]
    F --> G[Attend + admin marks attendance]
```

## One-Click Registration
When an admin creates or edits an event they choose how members register:

- **Custom form**: link a form built in the Form Builder (as before).
- **One-click**: tick which member details to collect (name and email are always included; phone, branch, roll number, college, year and Club ID are optional). No form to build.

How it works (`apps/events/quick_registration.py`): saving an event with `quick_registration_fields` creates (or updates) a normal published form made only of profile-bound fields and links it as the event's `registration_form`. The server fills every answer from the signed-in member's own profile (`FormField.profile_field`), so responses, exports, confirmation emails, attendance badges and duplicate prevention all work unchanged.

- The event's `capacity` becomes the form's `max_total_responses`, so registration closes by itself when the event is full. Renaming the event or changing its capacity keeps the form in step.
- A form counts as one-click when every active field is profile-bound. Adding an ordinary question to it in the Form Builder turns it into a custom form. The event API reports this as `is_quick_registration` and `registration_profile_fields`.
- Switching an event from a custom form to one-click creates a new form; the custom form and its responses are never modified.
- On the public pages a signed-in member registers with one click (`QuickRegisterButton` in the frontend). A signed-out visitor is sent to login and back. A member whose profile lacks a chosen detail is told which one and linked to their profile. Registering needs a signed-in account.

## Key Features Used
- [Dynamic Form Builder](../features/dynamic-form-builder.md)
- [Feature Flags](../features/feature-flags.md) — module + date-based visibility
- [Scheduling](../features/scheduling.md) — auto-archive past events, reminder timing
- [Custom Email & Notifications](../features/email-notifications.md) — confirmations, reminders
- [Data Export](../features/data-export.md) — attendee lists for check-in
- [Analytics & Insights](../features/analytics-insights.md) — attendance/engagement stats

## Data Captured
Attendee name, contact, college/branch/year (or custom fields per event), attendance status.

## Roles Involved
Member (attendee), Club Lead/Admin (organizer), Volunteer (check-in support).

## Related Docs
- [../admin/event-hackathon-management.md](../admin/event-hackathon-management.md)
- [hackathon.md](hackathon.md) — for hackathon-specific events, use that module instead
