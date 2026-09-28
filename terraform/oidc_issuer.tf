locals {
  oidc_issuer_enabled = var.oidc_issuer_enabled && local.custom_domains_enabled
  oidc_issuer_count   = local.oidc_issuer_enabled ? 1 : 0
  oidc_host           = "oidc.${local.host}"
  oidc_issuer_url     = "https://${local.oidc_host}"
  oidc_function_name  = "${local.prefix}-oidc"

  oidc_signing_keys  = local.oidc_issuer_enabled ? toset(var.oidc_signing_key_generations) : toset([])
  oidc_active_key    = local.oidc_issuer_enabled ? aws_kms_key.oidc_signing[var.oidc_signing_key_generations[length(var.oidc_signing_key_generations) - 1]].arn : ""
  oidc_published_ids = [for label in reverse(var.oidc_signing_key_generations) : aws_kms_key.oidc_signing[label].key_id if local.oidc_issuer_enabled]
}

data "aws_iam_policy_document" "oidc_signing_key" {
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
    sid       = "OnlyTheRunsFunctionSigns"
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
      values   = [local.runs_lambda_role_arn]
    }
  }
}

resource "aws_kms_key" "oidc_signing" {
  for_each = local.oidc_signing_keys

  description              = "Signs the workload identity tokens the ${local.prefix} OIDC issuer vouches for, generation ${each.key}. Only the runs function may sign"
  key_usage                = "SIGN_VERIFY"
  customer_master_key_spec = "RSA_2048"
  deletion_window_in_days  = 30
  policy                   = data.aws_iam_policy_document.oidc_signing_key.json

  tags = { Component = "oidc-issuer" }
}

resource "aws_kms_alias" "oidc_signing" {
  for_each = local.oidc_signing_keys

  name          = "alias/${local.prefix}-oidc-signing-${each.key}"
  target_key_id = aws_kms_key.oidc_signing[each.key].key_id
}

data "archive_file" "oidc_issuer" {
  count = local.oidc_issuer_count

  type        = "zip"
  source_file = "${path.module}/oidc_issuer/handler.py"
  output_path = "${path.module}/.terraform/oidc_issuer.zip"
}

data "aws_iam_policy_document" "oidc_issuer_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "oidc_issuer" {
  count = local.oidc_issuer_count

  name               = "${local.oidc_function_name}-lambda"
  description        = "Serves the OIDC issuer's discovery document and JWKS. Reads the signing keys' public halves and nothing else"
  assume_role_policy = data.aws_iam_policy_document.oidc_issuer_trust.json

  tags = { Component = "oidc-issuer" }
}

resource "aws_cloudwatch_log_group" "oidc_issuer" {
  count = local.oidc_issuer_count

  name              = "/aws/lambda/${local.oidc_function_name}"
  retention_in_days = 7

  tags = { Component = "oidc-issuer" }
}

resource "aws_iam_role_policy" "oidc_issuer" {
  count = local.oidc_issuer_count

  name = "runtime"
  role = aws_iam_role.oidc_issuer[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "WriteOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.oidc_issuer[0].arn}:*"
      },
      {
        Sid      = "ReadTheSigningKeysPublicHalves"
        Effect   = "Allow"
        Action   = ["kms:GetPublicKey"]
        Resource = [for key in aws_kms_key.oidc_signing : key.arn]
      },
    ]
  })
}

resource "aws_lambda_function" "oidc_issuer" {
  count = local.oidc_issuer_count

  function_name = local.oidc_function_name
  description   = "The control plane's OIDC issuer: discovery document and JWKS for Google and Azure workload identity federation"
  role          = aws_iam_role.oidc_issuer[0].arn

  runtime       = "python3.13"
  handler       = "handler.handler"
  architectures = ["arm64"]
  memory_size   = 128
  timeout       = 10

  reserved_concurrent_executions = 10

  filename         = data.archive_file.oidc_issuer[0].output_path
  source_code_hash = data.archive_file.oidc_issuer[0].output_base64sha256

  environment {
    variables = {
      ISSUER          = local.oidc_issuer_url
      SIGNING_KEY_IDS = join(",", local.oidc_published_ids)
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.oidc_issuer[0].name
  }

  tags = { Component = "oidc-issuer" }

  depends_on = [aws_iam_role_policy.oidc_issuer]
}

module "oidc_certificate" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/acm-certificate"
  version = "~> 2.33"

  providers = {
    aws         = aws
    aws.records = aws.dns
  }

  enabled     = local.oidc_issuer_enabled
  domain_name = local.oidc_host
  zone_id     = local.records_zone_id

  depends_on = [module.staging_dns]
}

module "oidc_api" {
  count = local.oidc_issuer_count

  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/http-api"
  version = "~> 2.33"

  name        = "${local.prefix}-oidc"
  description = "The control plane's OIDC issuer. Anonymous by design and never behind the access gate: Google and Azure fetch it unauthenticated"

  integrations = {
    oidc = {
      lambda_function_name = aws_lambda_function.oidc_issuer[0].function_name
      lambda_invoke_arn    = aws_lambda_function.oidc_issuer[0].invoke_arn
    }
  }

  default_integration = null

  routes = {
    "GET /.well-known/openid-configuration" = { integration = "oidc", authorization_type = "NONE" }
    "GET /.well-known/jwks.json"            = { integration = "oidc", authorization_type = "NONE" }
  }

  throttling_burst_limit = 50
  throttling_rate_limit  = 20

  access_log_retention_days = 7

  lambda_permission_statement_id = "AllowAPIGatewayInvoke"

  disable_execute_api_endpoint = true
  authorizer_id                = null

  domain_name     = local.oidc_host
  certificate_arn = module.oidc_certificate.certificate_arn
}

resource "aws_route53_record" "oidc" {
  count    = local.oidc_issuer_count
  provider = aws.dns

  zone_id = local.records_zone_id
  name    = local.oidc_host
  type    = "A"

  alias {
    name                   = module.oidc_api[0].custom_domain_target_domain_name
    zone_id                = module.oidc_api[0].custom_domain_hosted_zone_id
    evaluate_target_health = false
  }
}
