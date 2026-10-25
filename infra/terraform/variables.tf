variable "aws_region" {
  description = "AWS region for all resources in this stack."
  type        = string
  default     = "ap-south-1"

  validation {
    condition     = var.aws_region == "ap-south-1"
    error_message = "This stack is defined for ap-south-1 only."
  }
}

variable "aws_profile" {
  description = "Local AWS CLI / IAM Identity Center profile. Never put access keys in Terraform files."
  type        = string
  default     = "terraform-dev"
}

variable "project_name" {
  description = "Short name used in resource names and tags."
  type        = string
  default     = "srkrcc"

  validation {
    condition     = can(regex("^[a-z0-9-]+$", var.project_name))
    error_message = "project_name must be lowercase letters, digits, or hyphens."
  }
}

variable "environment" {
  description = "Deployment environment label. Default is development."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "ecr_repository_name" {
  description = "ECR repository name. Default is shared across environments (srkrcc-backend)."
  type        = string
  default     = "srkrcc-backend"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-/]*$", var.ecr_repository_name))
    error_message = "ecr_repository_name must be a valid ECR name."
  }
}

variable "s3_bucket_name" {
  description = "Full S3 bucket name. Leave empty to derive project-environment-media-accountid (must be globally unique)."
  type        = string
  default     = ""

  validation {
    condition     = var.s3_bucket_name == "" || can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.s3_bucket_name))
    error_message = "s3_bucket_name must be empty or a valid S3 bucket name."
  }
}

variable "lambda_image_uri" {
  description = "Full ECR image URI including tag. Leave empty until an image has been pushed; Lambda is then not created."
  type        = string
  default     = ""
}

variable "lambda_memory" {
  description = "Lambda memory in MB. 1024 is enough for a development Gunicorn process."
  type        = number
  default     = 1024

  validation {
    condition     = var.lambda_memory >= 512 && var.lambda_memory <= 2048
    error_message = "lambda_memory must be between 512 and 2048."
  }
}

variable "lambda_timeout" {
  description = "Lambda timeout in seconds."
  type        = number
  default     = 60

  validation {
    condition     = var.lambda_timeout >= 10 && var.lambda_timeout <= 120
    error_message = "lambda_timeout must be between 10 and 120."
  }
}

variable "enable_s3_storage" {
  description = "Set Django USE_S3. The bucket is still created; this only toggles the application flag."
  type        = bool
  default     = false
}

variable "function_url_authorization_type" {
  description = "Lambda Function URL auth. NONE means any HTTPS client can reach Django. AWS_IAM requires SigV4."
  type        = string
  default     = "NONE"

  validation {
    condition     = contains(["NONE", "AWS_IAM"], var.function_url_authorization_type)
    error_message = "function_url_authorization_type must be NONE or AWS_IAM."
  }
}

variable "allowed_hosts" {
  description = "Django ALLOWED_HOSTS. Include the Function URL host after the first deploy."
  type        = string
  default     = ".lambda-url.ap-south-1.on.aws,localhost,127.0.0.1"
}

variable "cors_allowed_origins" {
  description = "Browser origins allowed to call the Function URL. Do not invent Amplify hosts."
  type        = string
  default     = "http://localhost:3000"
}

variable "csrf_trusted_origins" {
  description = "Django CSRF_TRUSTED_ORIGINS."
  type        = string
  default     = "http://localhost:3000"
}

variable "frontend_url" {
  description = "Public frontend origin used in emails. Localhost until Amplify exists."
  type        = string
  default     = "http://localhost:3000"
}

variable "email_provider" {
  description = "Django EMAIL_PROVIDER on Lambda. gmail sends through the club mailbox."
  type        = string
  default     = "gmail"

  validation {
    condition     = contains(["console", "resend", "gmail"], var.email_provider)
    error_message = "email_provider must be console, resend, or gmail."
  }
}

variable "email_host_user" {
  description = "Gmail address Lambda uses as EMAIL_HOST_USER."
  type        = string
  default     = ""
}

variable "email_host_password" {
  description = "Google app password. Set only in terraform.tfvars, which is gitignored. The value is stored in Terraform state."
  type        = string
  sensitive   = true
  default     = ""
}

variable "default_from_email" {
  description = "From header for mail sent by Lambda. Use the same Gmail address unless it is a verified Send mail as alias."
  type        = string
  default     = ""
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention. Shorter retention limits cost in development."
  type        = number
  default     = 14
}

variable "enable_form_uploads" {
  description = "Let dynamic-form file questions upload straight to the form-uploads bucket (FORM_UPLOADS_BACKEND=s3). Apply with false first, check the bucket, then switch on."
  type        = bool
  default     = false
}

variable "form_uploads_bucket_name" {
  description = "Optional fixed name for the form-uploads bucket. Empty uses <prefix>-form-uploads-<account id>."
  type        = string
  default     = ""
}

variable "form_upload_tmp_retention_days" {
  description = "Days before an upload nobody attached to a submission is removed by S3 (the app sweeps sooner). Keep above FORM_UPLOAD_PENDING_HOURS / 24."
  type        = number
  default     = 8

  validation {
    condition     = var.form_upload_tmp_retention_days >= 2
    error_message = "Keep at least 2 days so drafts holding an upload are not broken."
  }
}

variable "form_upload_noncurrent_days" {
  description = "Days an old version of a deleted or replaced form file is kept before it is erased for good."
  type        = number
  default     = 7
}
