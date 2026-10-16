terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }

  # Local state only in this phase. Do not add an S3 backend until a dedicated
  # state bucket and lock exist (created in a later, separate apply).
}
