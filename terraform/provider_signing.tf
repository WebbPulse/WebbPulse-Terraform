locals {
  provider_repo_subject_prefix = "repo:WebbPulse@${local.github_org_id}/terraform-provider-webbpulse@1379114669"

  provider_signing_parameter_prefix = "/${local.prefix}/provider-signing"

  provider_uploads_prefix = "registry/provider-uploads/"

  provider_signing_parameters = {
    public_key = "${local.provider_signing_parameter_prefix}/public-key"
    key_id     = "${local.provider_signing_parameter_prefix}/key-id"
  }
}

data "aws_iam_policy_document" "provider_signing_kms_key" {
  statement {
    sid       = "AccountAdministersTheKey"
    effect    = "Allow"
    actions   = ["kms:*"]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }

  statement {
    sid       = "OnlyTheReleaseWorkflowSigns"
    effect    = "Deny"
    actions   = ["kms:Sign"]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values   = [module.provider_release_role.role_arn]
    }
  }
}

resource "aws_kms_key" "provider_signing" {
  description              = "Signs terraform-provider-webbpulse release checksums for ${local.prefix} as an OpenPGP key. The private half never leaves KMS and only the provider release role may sign"
  key_usage                = "SIGN_VERIFY"
  customer_master_key_spec = "RSA_4096"
  deletion_window_in_days  = 30
  policy                   = data.aws_iam_policy_document.provider_signing_kms_key.json

  tags = { Component = "provider-signing" }
}

resource "aws_kms_alias" "provider_signing" {
  name          = "alias/${local.prefix}-provider-signing"
  target_key_id = aws_kms_key.provider_signing.key_id
}

resource "aws_ssm_parameter" "provider_signing" {
  for_each = local.provider_signing_parameters

  name           = each.value
  description    = "Public ${replace(each.key, "_", " ")} of the terraform-provider-webbpulse release signing key, written by the release workflow from the KMS signing key and served by the registry"
  type           = "String"
  tier           = "Standard"
  insecure_value = "pending"

  lifecycle {
    ignore_changes = [insecure_value]
  }
}

module "provider_release_role" {
  source  = "terraform.webbpulse.com/WebbPulse/platform-modules/aws//modules/github-actions-role"
  version = "~> 2.33"

  role_name        = "${local.prefix}-provider-release"
  role_description = "Signs provider releases with the KMS signing key, publishes its public key and uploads signed builds to the registry. Assumable only from the ${var.environment} environment of WebbPulse/terraform-provider-webbpulse."

  create_oidc_provider = false
  oidc_provider_arn    = module.github_actions_role.oidc_provider_arn

  subjects = ["${local.provider_repo_subject_prefix}:environment:${var.environment}"]

  inline_policy_name = "provider-release"

  policy_statements = [
    {
      sid       = "SignWithTheKmsKey"
      actions   = ["kms:Sign", "kms:GetPublicKey", "kms:DescribeKey"]
      resources = [aws_kms_key.provider_signing.arn]
    },
    {
      sid       = "PublishThePublicKey"
      actions   = ["ssm:GetParameter", "ssm:PutParameter"]
      resources = [for parameter in aws_ssm_parameter.provider_signing : parameter.arn]
    },
    {
      sid       = "UploadSignedBuilds"
      actions   = ["s3:PutObject"]
      resources = ["${module.artifacts.bucket_arn}/${local.provider_uploads_prefix}webbpulse/webbpulse/*"]
    },
    {
      sid       = "EncryptUploadsThroughS3"
      actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
      resources = [module.artifacts.kms_key_arn]
      condition = {
        StringEquals = { "kms:ViaService" = ["s3.${data.aws_region.current.region}.amazonaws.com"] }
      }
    },
  ]
}

resource "aws_cloudwatch_event_rule" "provider_upload_created" {
  name        = "${local.prefix}-provider-upload-created"
  description = "upload.json the provider release workflow writes last under registry/provider-uploads/"

  event_pattern = jsonencode({
    source      = ["aws.s3"]
    detail-type = ["Object Created"]
    detail = {
      bucket = { name = [module.artifacts.bucket] }
      object = { key = [{ wildcard = "${local.provider_uploads_prefix}*/upload.json" }] }
    }
  })

  tags = local.common_tags
}

resource "aws_cloudwatch_event_target" "provider_upload_created" {
  rule      = aws_cloudwatch_event_rule.provider_upload_created.name
  target_id = "registry-ingest-queue"
  arn       = module.registry_ingest.queue_arn

  input_transformer {
    input_paths = {
      bucket = "$.detail.bucket.name"
      key    = "$.detail.object.key"
    }

    input_template = <<-EOT
      {"kind":"provider_upload","bucket":"<bucket>","key":"<key>"}
    EOT
  }
}

data "aws_iam_policy_document" "registry_ingest_queue" {
  statement {
    sid       = "LetEventBridgeDeliverProviderUploads"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [module.registry_ingest.queue_arn]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.provider_upload_created.arn]
    }
  }
}

resource "aws_sqs_queue_policy" "registry_ingest" {
  queue_url = module.registry_ingest.queue_url
  policy    = data.aws_iam_policy_document.registry_ingest_queue.json
}
