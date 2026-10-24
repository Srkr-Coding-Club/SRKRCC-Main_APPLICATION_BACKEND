# Feature: Scalable & Secure

## What It Is
Not a single button or page — this is a design principle behind the platform: it's built with modern technologies chosen so it stays fast and safe as the club (and its data) grows, from a handful of members to thousands.

## What Makes It Scalable
- **One codebase, many modules** — new club activities become new modules, not new platforms to maintain. See [../architecture/README.md](../architecture/README.md).
- **No-new-table forms** — the [Dynamic Form Builder](dynamic-form-builder.md) means adding the 500th form is as cheap as the 1st. See [../architecture/data-model-dynamic-forms.md](../architecture/data-model-dynamic-forms.md).
- **Background jobs off the request cycle** — bulk emails and large DMC exports run on a background thread (`apps/core/tasks.py::run_in_background`) so they don't hold up the request that triggered them. This is plain Python threading, not Redis/Celery — see [../architecture/tech-stack.md](../architecture/tech-stack.md#background-jobs--caching-current-state) for why, and [scheduling.md](scheduling.md) for what's actually scheduled vs. still-aspirational.
- **CDN-backed file storage (Cloudflare R2)** — images and files load fast regardless of how many members are viewing them at once.
- **Free-tier-friendly hosting** — the platform can grow from ₹0/month to a few hundred/month only once usage genuinely justifies it. See [../architecture/deployment-infra.md](../architecture/deployment-infra.md).

## What Makes It Secure
- **JWT Authentication** — verified identity on every request.
- **Role Based Access Control** — every action is permission-checked. See [role-based-access.md](role-based-access.md).
- **Data Encryption** — sensitive data protected at rest.
- **Rate Limiting** — protects against abuse (spam registrations, brute-force login attempts).
- **Audit Logs** — every sensitive admin action is traceable. See [audit-logs.md](audit-logs.md).
- **Regular, redundant backups** — daily database backups, weekly file backups, copied to a second provider. See [../architecture/backup-security.md](../architecture/backup-security.md).

## Rate Limits (and why they are shaped this way)

Many students reach the site through one campus address, so a limit counted "per address" would lock a whole lab out together. The limits are therefore layered (`config/throttle_config.py`, `apps/core/throttling.py`):

- **Signed-in people are counted per account**, never per address (`user`, `form_submit`, `form_draft`, ...). A lab of 100 members each has their own budget.
- **Anonymous traffic is counted per address and is generous** (`anon` 600/min, `college_search` 900/min, `register` 120/hour, `login` 120/min, `form_submit_anon` 120/min): enough for a lab, still low enough to stop a flood.
- **Password guessing is stopped per account and address** (`login_account` 10/min): one person mistyping is paused briefly, a lab-mate on the same address is not affected, and a stranger elsewhere cannot lock someone out.
- **The real visitor address** comes from the Next.js proxy (`X-Client-IP` plus a shared secret); see `apps/core/client_ip.py`. Without `CLIENT_IP_SHARED_SECRET` set on both sides, every visitor looks like the proxy and shares one budget.
- **A throttled request** answers HTTP 429 with `{detail, code: "THROTTLED", retry_after}` and a `Retry-After` header; the website shows a friendly wait message, retries reads once automatically and counts down on the form's submit button.
- Every rate can be changed without code with a `THROTTLE_<SCOPE>` environment variable (see `.env.example`). For a big sign-up event raise `THROTTLE_REGISTER`, `THROTTLE_ANON` and `THROTTLE_FORM_SUBMIT_ANON`.

Counters live in the Django cache. On AWS Lambda a per-process cache would give every container its own count, so keep the shared database cache (the default outside local development) or move to Redis if traffic grows.

## Related Docs
- [../architecture/README.md](../architecture/README.md)
- [../architecture/backup-security.md](../architecture/backup-security.md)
- [role-based-access.md](role-based-access.md)
