# Feature: Data Export

## What It Is
A one-click way for Admins/Club Leads to export registrations, form responses, and analytics data to **CSV or Excel** — for offline use like printing check-in sheets, sharing with sponsors, or archiving.

## Why It Exists
Not everything happens on a screen — event-day check-in often works better from a printed or offline spreadsheet, sponsors may ask for participant demographics in Excel, and club records need periodic archiving outside the live database. Rebuilding this from scratch for every module would be wasteful, so it's a shared capability across the platform.

## How It Works
1. Admin navigates to any list of responses/registrations (e.g. an event's attendee list, a hackathon's registrations, Career applicants).
2. Clicks **Export**.
3. Chooses format (CSV or Excel) and, if relevant, which fields/columns to include.
4. Download starts — includes every response captured through the [Dynamic Form Builder](dynamic-form-builder.md) for that item.

## Where It's Used

| Module | What gets exported |
|---|---|
| [Events](../modules/events.md) | Attendee lists for check-in |
| [Hackathons](../modules/hackathon.md) / [IconCoders](../modules/iconcoders.md) | Team registrations, submissions metadata |
| [Career](../modules/career.md) | Applicant lists for club-run drives |
| [Codequest](../modules/codequest.md) | Submission history |
| Admin Analytics | Raw data behind any dashboard chart |

## Who Can Export
Governed by [Role Based Access](role-based-access.md) — generally Admin and Club Lead (for content they organize); Volunteers typically have view-only access without export rights, unless explicitly granted.

## Related Docs
- [dynamic-form-builder.md](dynamic-form-builder.md) — source of the data being exported
- [analytics-insights.md](analytics-insights.md)
- [audit-logs.md](audit-logs.md) — exports are logged as an action, since they can contain personal data
