# Deployment & Estimated Cost

The platform is designed to run **entirely on free tiers** while the club is small, and scale up affordably as membership grows.

## Current Deployment (what's actually running today)

Per `render.yaml` (backend) and the frontend's own deploy config — this is the
real, current setup, not the target/future one described further below:

| Component | Actually running |
|---|---|
| Backend | Render (single `web` service — `gunicorn`, `render.yaml`). No separate worker/beat service. |
| Database | Render PostgreSQL (`render.yaml`'s `databases:` block). |
| Cache | **None.** No `CACHES` setting in `config/settings.py` — Django's default in-process `LocMemCache` backs the one thing that uses caching (password-setup rate limiting). |
| Background jobs | **None separately deployed.** Plain Python threads inside the same web process — see [tech-stack.md](../architecture/tech-stack.md#background-jobs--caching-current-state). |
| File storage | Django's default local filesystem storage — no Cloudflare R2 / S3 config exists in `settings.py` or `requirements.txt`. On a host with an ephemeral filesystem (like Render's free tier), uploaded files do not reliably survive a redeploy — a real gap if user-uploaded files (signatures, profile photos) matter long-term. |
| Email | Django's `EmailMultiAlternatives`, console backend in dev / SMTP in prod — no Resend/Brevo integration exists yet. |
| Monitoring | None configured (no Sentry/PostHog in `requirements.txt`). |

## Target / Future Deployment (aspirational — not yet built)

The table and flow below describe where the team intends to take this as the
club scales past free-tier limits. Treat every row as a plan, not a fact about
the running system.

| Component | Provider (free tier) |
|---|---|
| Frontend | Vercel or Cloudflare Pages |
| Backend | Oracle Cloud Always Free / Render / Fly.io / Railway |
| Database | Neon or Supabase (PostgreSQL) |
| Storage | Cloudflare R2 |
| Cache | Upstash Redis |
| Email | Resend / Brevo (free tier) |
| Monitoring | Sentry / UptimeRobot (free) |
| CI/CD | GitHub Actions |

## Estimated Cost

**Today, on Render's free tier:** ₹0/month — this is the actual current cost, not a projection.

**Target, after growth (10,000+ users, 100,000+ registrations), once the aspirational multi-provider setup above is built out:** approximately **₹500 – ₹2,000/month**, mainly from database and storage exceeding free-tier limits.

## Why this matters for the club

- No committee needs to approve a hosting budget to launch or run pilot events.
- Costs only start appearing once the platform is genuinely successful (high usage), at which point the club can budget for it.
- The target architecture above is deliberately provider-agnostic (e.g. Redis via Upstash, Postgres via Neon/Supabase) so the club isn't locked into one vendor if pricing changes — but none of that is provisioned yet; today it's a single Render web service and a single Render Postgres database.

## Continuous Integration / Deployment

**Target design, not current**: no `.github/workflows/` directory exists in either repo today, so there is no automated CI (tests/lint/build checks) or automated deploy pipeline yet. Render deploys directly from a pushed branch (its own built-in git integration), which is not the same as a GitHub Actions pipeline running checks first.

## Related Docs
- [README.md](README.md) — architecture overview
- [tech-stack.md](tech-stack.md) — technology choices
- [backup-security.md](backup-security.md) — backups and security
