locals {
  runner_image = "${module.registry.repository_urls["runner"]}:${var.runner_image_tag}"

  runner_task_statements = concat(
    [
      {
        Sid      = "ReportPhaseOutcome"
        Effect   = "Allow"
        Action   = ["states:SendTaskSuccess", "states:SendTaskFailure", "states:SendTaskHeartbeat"]
        Resource = ["*"]
      },
      {
        Sid      = "WriteRunnerLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["${aws_cloudwatch_log_group.runner.arn}:*"]
      },
      {
        Sid      = "AssumeAnyWorkspaceRunRole"
        Effect   = "Allow"
        Action   = ["sts:AssumeRole", "sts:TagSession"]
        Resource = ["arn:aws:iam::*:role/${local.prefix}-workspace-*"]
      },
    ],
    local.bucket_statements["State"],
    local.bucket_statements["Artifacts"],
  )
}

module "runner" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/ecs-fargate"
  version = "2.24.0"

  cluster_name = "${local.prefix}-runner"

  tasks = {
    for phase in ["plan", "apply"] : phase => {
      image  = local.runner_image
      cpu    = var.runner_task_cpu
      memory = var.runner_task_memory

      architecture = "ARM64"

      ephemeral_storage_size = 40

      environment = {
        TF_IN_AUTOMATION = "true"
        ENVIRONMENT      = var.environment
        AWS_REGION_NAME  = var.aws_region
        PHASE            = phase
        RUNNER_LOG_GROUP = aws_cloudwatch_log_group.runner.name
      }

      task_policy_statements = local.runner_task_statements

      log_retention_days = 30
    }
  }

  tags = { Component = "runner" }
}
