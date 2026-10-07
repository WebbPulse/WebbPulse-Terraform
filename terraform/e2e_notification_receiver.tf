locals {
  e2e_notification_receiver_count = var.environment == "staging" ? 1 : 0
  e2e_notification_receiver_name  = "${local.prefix}-e2e-notification-receiver"
}

data "archive_file" "e2e_notification_receiver" {
  count = local.e2e_notification_receiver_count

  type        = "zip"
  source_file = "${path.module}/e2e_notification_receiver/handler.py"
  output_path = "${path.module}/.terraform/e2e_notification_receiver.zip"
}

data "aws_iam_policy_document" "e2e_notification_receiver_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "e2e_notification_receiver" {
  count = local.e2e_notification_receiver_count

  name               = "${local.e2e_notification_receiver_name}-lambda"
  description        = "Staging only e2e receiver for generic run notifications. Writes its own logs and nothing else"
  assume_role_policy = data.aws_iam_policy_document.e2e_notification_receiver_trust.json

  tags = { Component = "e2e-notification-receiver" }
}

resource "aws_cloudwatch_log_group" "e2e_notification_receiver" {
  count = local.e2e_notification_receiver_count

  name              = "/aws/lambda/${local.e2e_notification_receiver_name}"
  retention_in_days = 7

  tags = { Component = "e2e-notification-receiver" }
}

resource "aws_iam_role_policy" "e2e_notification_receiver" {
  count = local.e2e_notification_receiver_count

  name = "runtime"
  role = aws_iam_role.e2e_notification_receiver[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "WriteOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.e2e_notification_receiver[0].arn}:*"
      },
    ]
  })
}

resource "aws_lambda_function" "e2e_notification_receiver" {
  count = local.e2e_notification_receiver_count

  function_name    = local.e2e_notification_receiver_name
  description      = "Staging only: answers a generic run notification with whether its signature matched"
  role             = aws_iam_role.e2e_notification_receiver[0].arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "handler.handler"
  filename         = data.archive_file.e2e_notification_receiver[0].output_path
  source_code_hash = data.archive_file.e2e_notification_receiver[0].output_base64sha256
  memory_size      = 128
  timeout          = 5

  reserved_concurrent_executions = 5

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.e2e_notification_receiver[0].name
  }

  tags = { Component = "e2e-notification-receiver" }

  depends_on = [aws_iam_role_policy.e2e_notification_receiver]
}

resource "aws_lambda_function_url" "e2e_notification_receiver" {
  count = local.e2e_notification_receiver_count

  function_name      = aws_lambda_function.e2e_notification_receiver[0].function_name
  authorization_type = "NONE"
}

resource "aws_lambda_permission" "e2e_notification_receiver_url" {
  count = local.e2e_notification_receiver_count

  statement_id           = "AllowPublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.e2e_notification_receiver[0].function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "e2e_notification_receiver_invoke" {
  count = local.e2e_notification_receiver_count

  statement_id             = "AllowPublicInvokeThroughFunctionUrl"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.e2e_notification_receiver[0].function_name
  principal                = "*"
  invoked_via_function_url = true
}
