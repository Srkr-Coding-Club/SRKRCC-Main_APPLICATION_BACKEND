# Glossary

Plain-language definitions for terms used throughout these docs.

| Term | Meaning |
|---|---|
| **Module** | A distinct area of the platform (Home, Events, Hackathons, Career, Blog, etc.) that can be independently enabled/disabled. See [features/feature-flags.md](features/feature-flags.md). |
| **Feature Flag** | An on/off switch controlling whether a module or item is visible, without needing a code deployment. See [features/feature-flags.md](features/feature-flags.md). |
| **Visibility Window** | A `visible_from` / `visible_until` date range that exists as a database column on Events and Hackathons and is editable via the Event API — but isn't actually read/enforced anywhere yet (dead field, not a working feature). See [features/scheduling.md](features/scheduling.md). |
| **Dynamic Form** | A form built through the no-code Form Builder, stored as metadata rather than a dedicated database table. See [features/dynamic-form-builder.md](features/dynamic-form-builder.md). |
| **Form Field** | One question/input within a form (text, dropdown, file upload, etc.). |
| **Response** | One user's full submission of a form. |
| **Answer** | One field's value within a single response. |
| **RBAC (Role Based Access Control)** | The system that decides what each role (Member, Volunteer, Club Lead, Admin, etc.) is allowed to see/do. See [features/role-based-access.md](features/role-based-access.md). |
| **Scoped Role** | A role that only applies within one specific item, like a Judge for one hackathon or a Volunteer for one event, rather than platform-wide. |
| **Audit Log** | The automatic record of every important admin action (who, what, when). See [features/audit-logs.md](features/audit-logs.md). |
| **Scheduling** | The lazy, on-read system that flips a form/event's status once its `open_at`/`close_at` has passed. See [features/scheduling.md](features/scheduling.md). |
| **CDN (Content Delivery Network)** | Infrastructure that serves images/files quickly no matter where a user is located. |
| **IconCoders** | The club's flagship annual hackathon, built on the generic [Hackathons module](modules/hackathon.md). See [modules/iconcoders.md](modules/iconcoders.md). |
| **Codequest** | The club's daily "Problem of the Day" module. See [modules/codequest.md](modules/codequest.md). |
| **Celery / Redis** | **Not currently used.** Referenced in older docs and in `.env.example` (`CELERY_BROKER_URL`, `REDIS_URL`) as a possible future upgrade, but no Celery worker or Redis instance is deployed or required today — see [architecture/tech-stack.md](architecture/tech-stack.md#background-jobs--caching-current-state). |
| **Background thread** | The actual mechanism behind "async" work today (bulk emails, large exports): `apps/core/tasks.py::run_in_background` spawns a plain daemon `threading.Thread` — no task queue, no broker, no persistence across a process restart. |
| **Cloudflare R2** | Where all uploaded files/images are stored, served via CDN. |

## Related Docs
- [README.md](README.md) — start here
