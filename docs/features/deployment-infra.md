# Deployment & Estimated Cost

The platform is designed to run **entirely on free tiers** while the club is small, and scale up affordably as membership grows.

## Where things run

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

**During development / within free-tier limits:** ₹0/month across frontend, backend, database, storage, cache, email, monitoring, and domain.

**After growth (10,000+ users, 100,000+ registrations):** approximately **₹500 – ₹2,000/month**, mainly from database and storage exceeding free-tier limits.

## Why this matters for the club

- No committee needs to approve a hosting budget to launch or run pilot events.
- Costs only start appearing once the platform is genuinely successful (high usage), at which point the club can budget for it.
- Everything is provider-agnostic where possible (e.g. Redis via Upstash, Postgres via Neon/Supabase) so the club isn't locked into one vendor if pricing changes.

## Continuous Integration / Deployment

GitHub Actions runs automated checks and deployments whenever code is pushed, so new features (or fixes) reach the live site without manual server work.

## Related Docs
- [README.md](README.md) — architecture overview
- [tech-stack.md](tech-stack.md) — technology choices
- [backup-security.md](backup-security.md) — backups and security
