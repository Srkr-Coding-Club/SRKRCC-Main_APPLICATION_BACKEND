# Split by service: ecr.tf, iam.tf, s3.tf, lambda.tf.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
