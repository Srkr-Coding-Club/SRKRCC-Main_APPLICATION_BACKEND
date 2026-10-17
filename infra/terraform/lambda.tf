resource "aws_cloudwatch_log_group" "lambda" {
  name              = local.lambda_log_group_name
  retention_in_days = var.log_retention_days
}

# Created only after an image exists in ECR. Passing an empty lambda_image_uri
# (the default) skips this resource so the first apply can create ECR/S3/IAM.
resource "aws_lambda_function" "api" {
  count = local.create_lambda_function ? 1 : 0

  function_name = local.lambda_function_name
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = var.lambda_image_uri
  timeout       = var.lambda_timeout
  memory_size   = var.lambda_memory
  architectures = ["x86_64"]

  ephemeral_storage {
    size = 512
  }

  environment {
    variables = local.lambda_environment
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

resource "aws_lambda_function_url" "api" {
  count = local.create_lambda_function ? 1 : 0

  function_name      = aws_lambda_function.api[0].function_name
  authorization_type = var.function_url_authorization_type

  cors {
    allow_credentials = true
    allow_origins     = local.cors_origins
    allow_methods     = ["*"]
    allow_headers     = ["*"]
    max_age           = 86400
  }
}
