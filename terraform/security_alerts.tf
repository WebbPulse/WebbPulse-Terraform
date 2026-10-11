locals {
  security_alerts_count = local.security_alerts_enabled ? 1 : 0
  security_alerts_name  = "${local.prefix}-security-alerts"

  security_alert_actions = [
    "api_key.created",
    "device_grant.opened",
    "run.confirmed",
    "variable.written",
    "variable.deleted",
    "workspace.updated",
    "workspace.plan_access_changed",
    "workspace.auto_apply_changed",
    "workspace.run_api_scopes_changed",
    "workspace.aws_connected",
    "github.app_created",
    "github.webhook_synced",
    "github.installation_recorded",
    "github.installation_removed",
  ]

  security_alerts_gate_log_group = "/aws/lambda/${local.access_gate_name}-access-gate-login"
  security_alerts_gate_count     = local.security_alerts_enabled && local.access_gate_enabled ? 1 : 0
}

data "archive_file" "security_alerts" {
  count = local.security_alerts_count

  type        = "zip"
  source_file = "${path.module}/security_alerts/handler.py"
  output_path = "${path.module}/.terraform/security_alerts.zip"
}

data "aws_iam_policy_document" "security_alerts_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_dynamodb_table" "security_alert_sightings" {
  count = local.security_alerts_count

  name         = "${local.prefix}-security-alert-sightings"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "email"
  range_key    = "marker"

  attribute {
    name = "email"
    type = "S"
  }

  attribute {
    name = "marker"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = { Component = "security-alerts" }
}

resource "aws_iam_role" "security_alerts" {
  count = local.security_alerts_count

  name               = "${local.security_alerts_name}-lambda"
  description        = "Production only owner alerts on sensitive plane events. Reads the audit stream, publishes to the alarm topic"
  assume_role_policy = data.aws_iam_policy_document.security_alerts_trust.json

  tags = { Component = "security-alerts" }
}

resource "aws_cloudwatch_log_group" "security_alerts" {
  count = local.security_alerts_count

  name              = "/aws/lambda/${local.security_alerts_name}"
  retention_in_days = 30

  tags = { Component = "security-alerts" }
}

resource "aws_iam_role_policy" "security_alerts" {
  count = local.security_alerts_count

  name = "runtime"
  role = aws_iam_role.security_alerts[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "WriteOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.security_alerts[0].arn}:*"
      },
      {
        Sid      = "ReadAuditStream"
        Effect   = "Allow"
        Action   = ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:ListStreams"]
        Resource = "${module.dynamodb.table_arns["audit"]}/stream/*"
      },
      {
        Sid      = "NameActorsAndWorkspaces"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem"]
        Resource = [module.dynamodb.table_arns["users"], module.dynamodb.table_arns["workspaces"]]
      },
      {
        Sid      = "RecordSightings"
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.security_alert_sightings[0].arn
      },
      {
        Sid      = "PublishAlerts"
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = module.alarms.sns_topic_arn
      },
    ]
  })
}

resource "aws_lambda_function" "security_alerts" {
  count = local.security_alerts_count

  function_name    = local.security_alerts_name
  description      = "Production only: emails the owner on sensitive plane events from the audit stream and gate sign-ins"
  role             = aws_iam_role.security_alerts[0].arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "handler.handler"
  filename         = data.archive_file.security_alerts[0].output_path
  source_code_hash = data.archive_file.security_alerts[0].output_base64sha256
  memory_size      = 128
  timeout          = 30

  environment {
    variables = {
      TOPIC_ARN        = module.alarms.sns_topic_arn
      USERS_TABLE      = module.dynamodb.table_names["users"]
      WORKSPACES_TABLE = module.dynamodb.table_names["workspaces"]
      SIGHTINGS_TABLE  = aws_dynamodb_table.security_alert_sightings[0].name
      SITE_URL         = "https://${local.host}"
      SUBJECT_PREFIX   = "WebbPulse Terraform"
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.security_alerts[0].name
  }

  tags = { Component = "security-alerts" }

  depends_on = [aws_iam_role_policy.security_alerts]
}

resource "aws_lambda_event_source_mapping" "security_alerts_audit" {
  count = local.security_alerts_count

  function_name                  = aws_lambda_function.security_alerts[0].arn
  event_source_arn               = module.dynamodb.stream_arns["audit"]
  starting_position              = "LATEST"
  batch_size                     = 10
  maximum_retry_attempts         = 3
  bisect_batch_on_function_error = true

  filter_criteria {
    filter {
      pattern = jsonencode({
        eventName = ["INSERT"]
        dynamodb = {
          NewImage = {
            action = { S = local.security_alert_actions }
          }
        }
      })
    }
  }
}

resource "aws_lambda_permission" "security_alerts_gate_logs" {
  count = local.security_alerts_gate_count

  statement_id   = "AllowGateLoginLogs"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.security_alerts[0].function_name
  principal      = "logs.amazonaws.com"
  source_arn     = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:log-group:${local.security_alerts_gate_log_group}:*"
  source_account = data.aws_caller_identity.current.account_id
}

resource "aws_cloudwatch_log_subscription_filter" "security_alerts_gate" {
  count = local.security_alerts_gate_count

  name            = "${local.security_alerts_name}-gate-sign-in"
  log_group_name  = local.security_alerts_gate_log_group
  filter_pattern  = "\"session issued\""
  destination_arn = aws_lambda_function.security_alerts[0].arn

  depends_on = [aws_lambda_permission.security_alerts_gate_logs, module.access_gate]
}
