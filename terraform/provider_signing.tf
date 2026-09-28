locals {
  provider_repo_subject_prefix = "repo:WebbPulse@${local.github_org_id}/terraform-provider-webbpulse@1379114669"

  provider_signing_parameter_prefix = "/${local.prefix}/provider-signing"

  provider_signing_parameters = {
    public_key = "${local.provider_signing_parameter_prefix}/public-key"
    key_id     = "${local.provider_signing_parameter_prefix}/key-id"
  }
}

resource "aws_secretsmanager_secret" "provider_signing_key" {
  name        = "${local.prefix}-provider-signing-key"
  description = "ASCII armored private GPG key that signs terraform-provider-webbpulse releases for this environment. Written once by the provider repo's key generation workflow and read only by its release workflow."
}

data "aws_iam_policy_document" "provider_signing_key" {
  statement {
    sid       = "OnlyReleaseWorkflowReads"
    effect    = "Deny"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "StringNotEquals"
      variable = "aws:PrincipalArn"
      values   = [module.provider_release_role.role_arn]
    }
  }

  statement {
    sid       = "OnlyKeyGenerationWrites"
    effect    = "Deny"
    actions   = ["secretsmanager:PutSecretValue", "secretsmanager:UpdateSecretVersionStage"]
    resources = ["*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "StringNotEquals"
      variable = "aws:PrincipalArn"
      values   = [module.provider_signing_keygen_role.role_arn]
    }
  }
}

resource "aws_secretsmanager_secret_policy" "provider_signing_key" {
  secret_arn = aws_secretsmanager_secret.provider_signing_key.arn
  policy     = data.aws_iam_policy_document.provider_signing_key.json
}

resource "aws_ssm_parameter" "provider_signing" {
  for_each = local.provider_signing_parameters

  name           = each.value
  description    = "Public ${replace(each.key, "_", " ")} of the terraform-provider-webbpulse release signing key, written by the key generation workflow and served by the registry"
  type           = "String"
  tier           = "Standard"
  insecure_value = "pending"

  lifecycle {
    ignore_changes = [insecure_value]
  }
}

module "provider_signing_keygen_role" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/github-actions-role"
  version = "~> 2.33"

  role_name        = "${local.prefix}-provider-signing-keygen"
  role_description = "Writes the provider release signing key generated in CI. Assumable only from the ${var.environment}-signing-key environment of WebbPulse/terraform-provider-webbpulse."

  create_oidc_provider = false
  oidc_provider_arn    = module.github_actions_role.oidc_provider_arn

  subjects = ["${local.provider_repo_subject_prefix}:environment:${var.environment}-signing-key"]

  inline_policy_name = "provider-signing-keygen"

  policy_statements = [
    {
      sid       = "WriteSigningKey"
      actions   = ["secretsmanager:PutSecretValue", "secretsmanager:DescribeSecret"]
      resources = [aws_secretsmanager_secret.provider_signing_key.arn]
    },
    {
      sid       = "PublishPublicKey"
      actions   = ["ssm:PutParameter"]
      resources = [for parameter in aws_ssm_parameter.provider_signing : parameter.arn]
    },
  ]
}

module "provider_release_role" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/github-actions-role"
  version = "~> 2.33"

  role_name        = "${local.prefix}-provider-release"
  role_description = "Reads the provider release signing key. Assumable only from the ${var.environment} environment of WebbPulse/terraform-provider-webbpulse."

  create_oidc_provider = false
  oidc_provider_arn    = module.github_actions_role.oidc_provider_arn

  subjects = ["${local.provider_repo_subject_prefix}:environment:${var.environment}"]

  inline_policy_name = "provider-release"

  policy_statements = [
    {
      sid       = "ReadSigningKey"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = [aws_secretsmanager_secret.provider_signing_key.arn]
    },
  ]
}
