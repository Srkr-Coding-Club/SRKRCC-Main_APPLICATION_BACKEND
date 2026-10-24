data "aws_iam_policy_document" "lambda_assume" {
  statement {
    sid     = "LambdaAssume"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = local.lambda_role_name
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "lambda" {
  statement {
    sid = "CloudWatchLogs"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      "${aws_cloudwatch_log_group.lambda.arn}:*",
    ]
  }

  statement {
    sid = "MediaBucket"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = ["${aws_s3_bucket.media.arn}/*"]
  }

  statement {
    sid = "FormUploadsBucket"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:GetObjectTagging",
    ]
    resources = ["${aws_s3_bucket.form_uploads.arn}/*"]
  }

  statement {
    sid       = "FormUploadsBucketList"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.form_uploads.arn]
  }

  statement {
    sid       = "MediaBucketList"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.media.arn]
  }

  statement {
    sid = "ReadDjangoSecrets"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
    ]
    resources = local.ssm_parameter_arns
  }

  # alias/aws/ssm is an AWS-managed key, not a customer-managed key created here.
  # Resource "*" is required because that key id is not known to this stack.
  # ViaService and the parameter ARN encryption context keep decrypt limited to
  # these two SecureStrings.
  statement {
    sid       = "DecryptParameterStore"
    actions   = ["kms:Decrypt"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${data.aws_region.current.name}.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "kms:EncryptionContext:PARAMETER_ARN"
      values   = local.ssm_parameter_arns
    }
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${local.lambda_role_name}-policy"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}
