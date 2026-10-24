locals {
  name_prefix = "${var.project_name}-${var.environment}"

  ecr_repository_name = var.ecr_repository_name

  # srkrcc-dev-backend
  lambda_function_name = "${local.name_prefix}-backend"

  # srkrcc-dev-backend-lambda-execution
  lambda_role_name = "${local.lambda_function_name}-lambda-execution"

  lambda_log_group_name = "/aws/lambda/${local.lambda_function_name}"

  # Globally unique unless the operator supplies a reserved name.
  media_bucket_name = var.s3_bucket_name != "" ? var.s3_bucket_name : "${local.name_prefix}-media-${data.aws_caller_identity.current.account_id}"

  # /srkrcc/dev/backend/...
  ssm_prefix            = "/${var.project_name}/${var.environment}/backend"
  ssm_secret_key_name   = "${local.ssm_prefix}/secret_key"
  ssm_database_url_name = "${local.ssm_prefix}/database_url"
  ssm_parameter_arns = [
    "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter${local.ssm_secret_key_name}",
    "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter${local.ssm_database_url_name}",
  ]

  create_lambda_function = var.lambda_image_uri != ""

  common_tags = {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "terraform"
    Purpose     = "django-lambda"
    Service     = "backend"
  }

  cors_origins = [
    for origin in split(",", var.cors_allowed_origins) : trimspace(origin) if trimspace(origin) != ""
  ]

  lambda_environment = {
    DEBUG                   = "False"
    SERVERLESS              = "1"
    USE_S3                  = var.enable_s3_storage ? "True" : "False"
    AWS_STORAGE_BUCKET_NAME = aws_s3_bucket.media.id
    AWS_S3_REGION_NAME      = var.aws_region
    ALLOWED_HOSTS           = var.allowed_hosts
    CORS_ALLOWED_ORIGINS    = var.cors_allowed_origins
    CSRF_TRUSTED_ORIGINS    = var.csrf_trusted_origins
    FRONTEND_URL            = var.frontend_url
    EMAIL_PROVIDER          = var.email_provider
    EMAIL_HOST_USER         = var.email_host_user
    EMAIL_HOST_PASSWORD     = var.email_host_password
    DEFAULT_FROM_EMAIL      = var.default_from_email
    SECURE_SSL_REDIRECT     = "False"
    # Names only. Django reads the values from SSM when serverless.
    # RUN_STARTUP_TASKS is omitted so entrypoint.sh does not migrate on cold start.
    SSM_SECRET_KEY_NAME   = local.ssm_secret_key_name
    SSM_DATABASE_URL_NAME = local.ssm_database_url_name
  }
}
