# Terraform foundation (Phase 4.1)

Defines ECR, a private media bucket, a Lambda execution role, and (optionally) a container Lambda + Function URL. **This directory has not been applied.** `terraform apply` is out of scope for this phase.

## Resource names (defaults for `project_name=srkrcc`, `environment=dev`)

| Resource | Default name | Source |
|---|---|---|
| ECR repository | `srkrcc-backend` | `var.ecr_repository_name` |
| Lambda function | `srkrcc-dev-backend` | `local.lambda_function_name` |
| Lambda execution role | `srkrcc-dev-backend-lambda-execution` | `local.lambda_role_name` |
| CloudWatch log group | `/aws/lambda/srkrcc-dev-backend` | `local.lambda_log_group_name` |
| S3 media bucket | `srkrcc-dev-media-<account-id>` | `local.media_bucket_name` (override with `s3_bucket_name`) |
| SSM prefix | `/srkrcc/dev/backend/` | `local.ssm_prefix` |

A later `environment=prod` stack would yield `srkrcc-prod-backend` and `/srkrcc/prod/backend/` without changing code. The ECR repository stays `srkrcc-backend` unless overridden. S3 names must be globally unique; do not assume `srkrcc-dev-media` is available.

## Commands

```bash
aws sso login --profile terraform-dev   # if the profile uses IAM Identity Center
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # edit non-secret values only
terraform init
terraform fmt
terraform validate
# Later, after review: terraform plan
# Later, after approval: terraform apply
```

Do not put `SECRET_KEY` or `DATABASE_URL` in `terraform.tfvars`. That file is gitignored; the example file is not.

## Apply order (later)

1. Apply with `lambda_image_uri = ""` (default). Creates ECR, S3, IAM, log group. Does **not** create Lambda.
2. Build and push the image from the backend repo root (not done in this phase).
3. Set `lambda_image_uri` to `repository-url:tag` and apply again.

Lambda cannot be created against an empty repository. Do not invent an image URI.

## Secrets (not in Terraform state)

Django on Lambda loads `SECRET_KEY` and `DATABASE_URL` from Parameter Store at process start (`config/ssm_secrets.py`). It does this only when `SERVERLESS` or `AWS_LAMBDA_FUNCTION_NAME` is set and the value is missing. Local and Render leave both flags unset and keep using their existing environment variables.

Terraform therefore:

- Does **not** create `aws_ssm_parameter` resources (a value would land in state).
- Passes only the parameter **names** to Lambda:
  - `SSM_SECRET_KEY_NAME=/srkrcc/dev/backend/secret_key`
  - `SSM_DATABASE_URL_NAME=/srkrcc/dev/backend/database_url`
- Grants the Lambda role `ssm:GetParameters` on those ARNs and `kms:Decrypt` through SSM for the AWS-managed `alias/aws/ssm` key.

If a parameter is missing, empty, or access is denied, Django raises `ImproperlyConfigured` and does not start. The error names the setting (`SECRET_KEY` or `DATABASE_URL`) and does not include the parameter value.

Create the SecureString values later with the CLI (Standard tier, not Advanced). On Git Bash set `MSYS_NO_PATHCONV=1` so `/srkrcc/...` is not rewritten:

```bash
MSYS_NO_PATHCONV=1 aws ssm put-parameter --profile terraform-dev --region ap-south-1 \
  --name /srkrcc/dev/backend/secret_key --type SecureString --value '...'
```

## Function URL security

Default `function_url_authorization_type = NONE`: any client that knows the HTTPS URL can send requests. That is **not** authentication.

- Django JWT / session still protect authenticated routes.
- Public routes (`/api/ping/`, public reads) are reachable by anyone.
- CORS is not auth. `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, and `CSRF_TRUSTED_ORIGINS` must be reviewed before sharing the URL.
- Do not invent an Amplify origin. Keep localhost until that host exists.
- Admin is at `/admin/` on the same origin; treat the Function URL as sensitive until hosts and auth are locked down.

`AWS_IAM` is the tighter alternative (SigV4). Browsers cannot sign that way without extra infrastructure.

## IAM

**Lambda execution role** (this stack): CloudWatch log streams on this function’s group, object R/W + list on the media bucket, SSM get on the two Django parameter names, and `kms:Decrypt` only when the call comes from SSM for those parameter ARNs.

**Not on the execution role:** `AdministratorAccess`, account-wide `*`, ECR pull. Lambda’s **service-linked role** pulls the container image from ECR. The principal that runs `terraform apply` (`terraform-dev`) is separate and must already be able to create these resources.

## State (later)

This phase uses **local state** only. Do not create an S3 state bucket here.

Recommended next step, in a **separate** bootstrap:

- Private, versioned, SSE-S3 (or SSE-KMS) bucket used **only** for Terraform state — not the media bucket.
- Block public access.
- Restrict IAM to operators who run Terraform.
- Locking: Terraform 1.10+ S3 `use_lockfile = true`, or a DynamoDB lock table.
- Never store `SECRET_KEY` / `DATABASE_URL` as Terraform resource attributes.

## Cost drivers (development, not free)

| Resource | What you pay for |
|---|---|
| Lambda | Invocations + GB-seconds after the always-free allowance. Memory and timeout are the knobs. |
| ECR | Image storage (~$0.10/GB). Lifecycle keeps 5 images. |
| S3 | Storage, requests, versioned objects. Public access is blocked. |
| CloudWatch Logs | Ingest + storage. Retention is 14 days. |
| Data transfer | Function URL egress to the internet. |
| SSM | Standard parameters are free. Do not switch to Advanced. |
| KMS | No customer-managed key. S3 and ECR use SSE-S3. SSM SecureString decrypt uses the AWS-managed `alias/aws/ssm` key, which has no monthly key charge. |

## Django flags this stack expects

| Env | Value on Lambda |
|---|---|
| `SERVERLESS` | `1` (no migrate on cold start; `conn_max_age=0`; `/tmp/media`) |
| `USE_S3` | `True` when `enable_s3_storage` (default true). Django default storage uses the media bucket. Backups still use local paths. |
| `FORM_UPLOADS_BACKEND` | `s3` when `enable_form_uploads` (default true). File questions, slides, and payment screenshots post straight to the form-uploads bucket. |
| `FORM_UPLOAD_BUCKET` | The form-uploads bucket id. Copy `form_uploads_bucket_origin` into the frontend `UPLOAD_CONNECT_SRC`. |
| `RUN_STARTUP_TASKS` | unset |
| `EMAIL_PROVIDER` | `var.email_provider` (`gmail` by default) |
| `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` / `DEFAULT_FROM_EMAIL` | Gmail SMTP. The app password is a sensitive Terraform variable, so it is in state. Django does not load it from SSM. Set it only in gitignored `terraform.tfvars`. |
| `FRONTEND_URL` | `var.frontend_url`, used for links inside email |

Backups and DMC exports still use local `open()` paths. They do not follow Django's storage backend, so `USE_S3` does not put them in the media bucket. On Lambda those files stay under `/tmp/media`.
