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
| Email | Django's `EmailMultiAlternatives` via `EMAIL_PROVIDER`: console in dev, Resend on staging, Gmail SMTP in prod. Per-environment variables are in `.env.staging.example` and `.env.production.example`; details in [email-notifications.md](email-notifications.md#email-providers). `render.yaml` does not define the email variables, so set them in the Render dashboard for each service. |
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
| Email | Resend for staging, Gmail SMTP for production (already wired up) |
| Monitoring | Sentry / UptimeRobot (free) |
| CI/CD | GitHub Actions |

## Estimated Cost

**Today, on Render's free tier:** ₹0/month — this is the actual current cost, not a projection.

**Target, after growth (10,000+ users, 100,000+ registrations), once the aspirational multi-provider setup above is built out:** approximately **₹500 – ₹2,000/month**, mainly from database and storage exceeding free-tier limits.

## Why this matters for the club

- No committee needs to approve a hosting budget to launch or run pilot events.
- Costs only start appearing once the platform is genuinely successful (high usage), at which point the club can budget for it.
- The target architecture above is deliberately provider-agnostic (e.g. Redis via Upstash, Postgres via Neon/Supabase) so the club isn't locked into one vendor if pricing changes — but none of that is provisioned yet; today it's a single Render web service and a single Render Postgres database.

## AWS Lambda (dev function `srkrcc-dev-backend`)

Render is unchanged: `build.sh` still runs `migrate` at build time, and the start command is Gunicorn with `SECRET_KEY` and `DATABASE_URL` from the Render environment. That path does not set `SERVERLESS` or `AWS_LAMBDA_FUNCTION_NAME`, so it never calls Parameter Store.

Lambda does not receive the database URL as an environment variable. `config/ssm_secrets.py` loads `SECRET_KEY` and `DATABASE_URL` from `/srkrcc/dev/backend/secret_key` and `/srkrcc/dev/backend/database_url` in `ap-south-1` before Django reads settings. `/api/ping/` does not query Postgres, so a healthy ping does not mean the database is reachable.

The image entrypoint runs `migrate`, `seed_flags`, and `setup_admin` only when `RUN_STARTUP_TASKS=1` (local Compose). Lambda leaves that unset, so a cold start does not create tables. `.github/workflows/deploy-lambda.yml` runs those commands once, after a commit lands on `main`, inside the new image. It sets `DATABASE_USE_DIRECT=1` so migrations use Neon's direct host (the `-pooler` host is PgBouncer and drops Django's session lock). The running function keeps the pooled URL, pins the socket to an IPv4 address, and does not send `statement_timeout` in the startup packet. Neon's pooler rejects that parameter, and its IPv6 addresses time out from Lambda.

Pull requests do not deploy. A manually pushed image will serve `/api/ping/` and still have an empty database until `main` is updated or migrations are applied by hand.

## Continuous Integration / Deployment

- Pull requests into `main` run `.github/workflows/ci.yml`: Django tests against Postgres 15, a migration check, and a local image build. The test job has no AWS credentials and does not touch Neon. The image job assumes the deploy role only to log in to ECR Public, so the Lambda adapter pull is not anonymous. The role needs `ecr-public:GetAuthorizationToken` and `sts:GetServiceBearerToken`.
- A push to `main` runs `.github/workflows/deploy-lambda.yml`: build, push to ECR, migrate, seed flags, create the admin if missing, update `srkrcc-dev-backend`, then ping the function URL.
- Render still deploys from its own git integration. That is separate from the Lambda workflow.

## Related Docs
- [README.md](README.md) — architecture overview
- [tech-stack.md](tech-stack.md) — technology choices
- [backup-security.md](backup-security.md) — backups and security
