# Admin: Audit Logs & Monitoring

Read [../features/audit-logs.md](../features/audit-logs.md) first for the concept — this page covers how to actually use the log and keep an eye on platform health.

## Reading the Audit Log
**Admin → Audit Logs** shows a searchable, filterable table:

| Column | Meaning |
|---|---|
| Timestamp | When the action happened |
| Actor | Which Admin/Club Lead performed it |
| Action | What happened (e.g. "Disabled module: Hackathons", "Changed role: Member → Club Lead") |
| Target | What was affected (module, user, form, event) |
| Before → After | The change itself, where applicable |

Filter by actor, action type, module, or date range — useful when something unexpected happened and you need to know why.

## When To Check the Audit Log
- A module or event disappeared and nobody remembers disabling it.
- A member's access level changed and they're asking why.
- A form or its responses look different than expected.
- Before/after a leadership handover, to review what the outgoing team changed.

## Platform Monitoring
Separate from the audit log (which tracks *admin actions*), the platform also has infrastructure monitoring for uptime/errors:

| Tool | Purpose |
|---|---|
| Sentry | Captures application errors so developers can fix bugs before many users notice |
| UptimeRobot | Alerts if the site goes down |

These are primarily for the technical/developer team, not day-to-day Club Lead use, but Admins should know they exist — see [../architecture/deployment-infra.md](../architecture/deployment-infra.md).

## Related Docs
- [../features/audit-logs.md](../features/audit-logs.md)
- [../architecture/backup-security.md](../architecture/backup-security.md)
