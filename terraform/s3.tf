module "state" {
  source  = "terraform.webbpulse.com/WebbPulse/platform-modules/aws//modules/s3-bucket"
  version = "~> 2.33"

  bucket = "${local.prefix}-state"

  create_kms_key      = true
  kms_key_description = "Encrypts Terraform state objects in ${local.prefix}-state"
  kms_key_alias       = "${local.prefix}-state"

  lifecycle_rules = {
    expire-noncurrent-state = {
      noncurrent_version_expiration_days     = 365
      newer_noncurrent_versions              = 10
      abort_incomplete_multipart_upload_days = 7
    }
  }
}

module "artifacts" {
  source  = "terraform.webbpulse.com/WebbPulse/platform-modules/aws//modules/s3-bucket"
  version = "~> 2.33"

  bucket = "${local.prefix}-artifacts"

  kms_key_arn = module.state.kms_key_arn

  enable_eventbridge_notifications = true

  lifecycle_rules = {
    expire-configs = {
      prefix                                 = "configs/"
      expiration_days                        = var.artifact_retention_days
      noncurrent_version_expiration_days     = 7
      abort_incomplete_multipart_upload_days = 7
    }
    expire-runs = {
      prefix                                 = "runs/"
      expiration_days                        = var.artifact_retention_days
      noncurrent_version_expiration_days     = 7
      abort_incomplete_multipart_upload_days = 7
    }
    expire-ingest = {
      prefix                                 = "ingest/"
      expiration_days                        = 3
      noncurrent_version_expiration_days     = 1
      abort_incomplete_multipart_upload_days = 1
    }
  }

  cors_rules = [
    {
      allowed_methods = ["PUT"]
      allowed_origins = [local.cors_origins]
      allowed_headers = ["*"]
      expose_headers  = ["ETag"]
      max_age_seconds = 3000
    },
  ]
}

moved {
  from = module.artifacts.aws_kms_key.this[0]
  to   = aws_kms_key.artifacts_retired
}

moved {
  from = module.artifacts.aws_kms_alias.this[0]
  to   = aws_kms_alias.artifacts_retired
}

resource "aws_kms_key" "artifacts_retired" {
  description              = "Encrypts config tarballs, plan artifacts and phase logs in ${local.prefix}-artifacts"
  key_usage                = "ENCRYPT_DECRYPT"
  customer_master_key_spec = "SYMMETRIC_DEFAULT"
  enable_key_rotation      = true
  deletion_window_in_days  = 30

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "EnableIAMPoliciesInThisAccount"
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
      Action    = "kms:*"
      Resource  = "*"
    }]
  })
}

resource "aws_kms_alias" "artifacts_retired" {
  name          = "alias/${local.prefix}-artifacts"
  target_key_id = aws_kms_key.artifacts_retired.key_id
}
