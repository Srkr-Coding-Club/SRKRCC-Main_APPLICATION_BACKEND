# Feature: Analytics & Insights

## What It Is
Dashboards showing how the platform and its content are being used — registrations over time, top colleges represented, active members, upcoming events, latest form submissions — so club leadership can make decisions with real numbers instead of guesses.

## Why It Exists
A club platform accumulates data constantly (registrations, submissions, page visits). Without a dashboard, that data just sits in the database, invisible. Analytics turns raw activity into something leadership can act on: "our hackathon registrations are trending up," "most participants come from these colleges," "this event's turnout was low — maybe don't repeat the format."

## What's Shown (Admin Dashboard)
Based on the platform's admin dashboard design:

| Metric / Panel | What it tells you |
|---|---|
| Total Users | Overall platform reach |
| Active Members | Members who've engaged recently |
| Events (count) | How many events have run |
| Registrations (count) | Total sign-ups across all modules |
| Registrations Overview (chart) | Trend of registrations over time |
| Top Colleges (chart) | Where participants are coming from |
| Recent Forms | Latest forms created |
| Upcoming Events | What's coming next |
| Latest Registrations | Most recent sign-ups, real-time |

## How It's Powered
- Core numbers come directly from the **PostgreSQL** database (counts, trends).
- Optional deeper behavioral tracking (page views, click paths) comes from **PostHog**, if enabled. See [../architecture/tech-stack.md](../architecture/tech-stack.md).

## Where It's Used
Every module feeds into the dashboards: [Events](../modules/events.md), [Hackathons](../modules/hackathon.md)/[IconCoders](../modules/iconcoders.md), [Codequest](../modules/codequest.md) (leaderboards/streaks), [Career](../modules/career.md), [Blog](../modules/blogs.md) (views/engagement).

## Related Docs
- [../admin/dashboard.md](../admin/dashboard.md) — the admin dashboard itself
- [data-export.md](data-export.md) — exporting the raw data behind any chart
