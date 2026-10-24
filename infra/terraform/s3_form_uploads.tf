# Files that people attach to dynamic-form questions. The browser uploads straight to this bucket with a presigned
# POST, so it is separate from the media bucket: its own CORS rule, its own lifecycle and nothing public.

resource "aws_s3_bucket" "form_uploads" {
  bucket = local.form_uploads_bucket_name
}

resource "aws_s3_bucket_ownership_controls" "form_uploads" {
  bucket = aws_s3_bucket.form_uploads.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "form_uploads" {
  bucket = aws_s3_bucket.form_uploads.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "form_uploads" {
  bucket = aws_s3_bucket.form_uploads.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# Versioning protects against an accidental overwrite, but a deleted file must really go: old versions expire.
resource "aws_s3_bucket_versioning" "form_uploads" {
  bucket = aws_s3_bucket.form_uploads.id

  versioning_configuration {
    status = "Enabled"
  }
}

# The browser posts to the bucket from the site, so the site origin must be allowed. Downloads are plain
# navigations to presigned links and need no CORS.
resource "aws_s3_bucket_cors_configuration" "form_uploads" {
  bucket = aws_s3_bucket.form_uploads.id

  cors_rule {
    allowed_origins = local.site_origins
    allowed_methods = ["POST", "GET"]
    allowed_headers = ["*"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3000
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "form_uploads" {
  bucket = aws_s3_bucket.form_uploads.id

  # A file that was uploaded but never attached to a submission. The app sweeps these sooner
  # (FORM_UPLOAD_PENDING_HOURS); this is the safety net if the sweep stops running.
  rule {
    id     = "expire-unattached-uploads"
    status = "Enabled"

    filter {
      prefix = "form-uploads/tmp/"
    }

    expiration {
      days = var.form_upload_tmp_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }

  # Deleting a file in the app deletes the current version; this removes the version history shortly after.
  rule {
    id     = "expire-deleted-versions"
    status = "Enabled"

    filter {
      prefix = "form-uploads/"
    }

    noncurrent_version_expiration {
      noncurrent_days = var.form_upload_noncurrent_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  depends_on = [aws_s3_bucket_versioning.form_uploads]
}

data "aws_iam_policy_document" "form_uploads_tls_only" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.form_uploads.arn,
      "${aws_s3_bucket.form_uploads.arn}/*",
    ]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "form_uploads" {
  bucket = aws_s3_bucket.form_uploads.id
  policy = data.aws_iam_policy_document.form_uploads_tls_only.json

  depends_on = [aws_s3_bucket_public_access_block.form_uploads]
}
