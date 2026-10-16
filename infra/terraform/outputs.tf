output "aws_account_id" {
  description = "Account that terraform-dev will target. Confirm before any later apply."
  value       = data.aws_caller_identity.current.account_id
}

output "ecr_repository_url" {
  description = "Push the Django image here after the first apply."
  value       = aws_ecr_repository.backend.repository_url
}

output "ecr_repository_name" {
  value = aws_ecr_repository.backend.name
}

output "lambda_function_name" {
  description = "Empty until lambda_image_uri is set."
  value       = local.create_lambda_function ? aws_lambda_function.api[0].function_name : ""
}

output "lambda_function_url" {
  description = "Public HTTPS URL when authorization_type is NONE. Empty until the function exists."
  value       = local.create_lambda_function ? aws_lambda_function_url.api[0].function_url : ""
}

output "media_bucket_name" {
  value = aws_s3_bucket.media.id
}

output "ssm_secret_key_name" {
  description = "Write SECRET_KEY here with the AWS CLI. Not created by Terraform."
  value       = local.ssm_secret_key_name
}

output "ssm_database_url_name" {
  description = "Write the Neon pooled DATABASE_URL here with the AWS CLI. Not created by Terraform."
  value       = local.ssm_database_url_name
}

output "lambda_role_name" {
  value = aws_iam_role.lambda.name
}

output "lambda_role_arn" {
  value = aws_iam_role.lambda.arn
}

output "ssm_prefix" {
  description = "Parameter Store prefix. Values are not managed by Terraform."
  value       = local.ssm_prefix
}
