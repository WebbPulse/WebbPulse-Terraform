module "run_task_failures" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/sqs-queue"
  version = "~> 2.27"

  name = "${local.prefix}-run-task-failures"

  consumer_timeout_seconds   = local.lambda_domain_timeout
  visibility_timeout_seconds = local.lambda_domain_timeout * 6
  max_receive_count          = 5
}

resource "aws_cloudwatch_event_rule" "runner_task_failed_to_start" {
  name        = "${local.prefix}-runner-task-failed-to-start"
  description = "Runner Fargate tasks that stopped without ever starting, so the run can be failed before its phase state times out"

  event_pattern = jsonencode({
    source      = ["aws.ecs"]
    detail-type = ["ECS Task State Change"]
    detail = {
      clusterArn = [module.runner.cluster_arn]
      lastStatus = ["STOPPED"]
      stopCode   = ["TaskFailedToStart"]
    }
  })

  tags = local.common_tags
}

resource "aws_cloudwatch_event_target" "runner_task_failed_to_start" {
  rule      = aws_cloudwatch_event_rule.runner_task_failed_to_start.name
  target_id = "run-task-failures-queue"
  arn       = module.run_task_failures.queue_arn

  input_transformer {
    input_paths = {
      detail = "$.detail"
    }

    input_template = <<-EOT
      {"kind":"run_task_failed_to_start","detail":<detail>}
    EOT
  }
}

data "aws_iam_policy_document" "run_task_failures_queue" {
  statement {
    sid       = "LetEventBridgeDeliverTaskStops"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [module.run_task_failures.queue_arn]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.runner_task_failed_to_start.arn]
    }
  }
}

resource "aws_sqs_queue_policy" "run_task_failures" {
  queue_url = module.run_task_failures.queue_url
  policy    = data.aws_iam_policy_document.run_task_failures_queue.json
}
