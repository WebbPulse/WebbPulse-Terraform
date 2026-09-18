module "state" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/s3-bucket"
  version = "2.24.0"

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
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/s3-bucket"
  version = "2.24.0"

  bucket = "${local.prefix}-artifacts"

  create_kms_key      = true
  kms_key_description = "Encrypts config tarballs, plan artifacts and phase logs in ${local.prefix}-artifacts"
  kms_key_alias       = "${local.prefix}-artifacts"

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
  }

  cors_rules = [
    {
      allowed_methods = ["PUT"]
      allowed_origins = local.custom_domains_enabled ? ["https://${local.host}"] : ["*"]
      allowed_headers = ["*"]
      expose_headers  = ["ETag"]
      max_age_seconds = 3000
    },
  ]
}
