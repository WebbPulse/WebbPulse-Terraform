locals {
  runner_image = "${module.registry.repository_urls["runner"]}:${var.runner_image_tag}"

  runner_task_statements = concat(
    [
      {
        sid       = "ReportPhaseOutcome"
        actions   = ["states:SendTaskSuccess", "states:SendTaskFailure", "states:SendTaskHeartbeat"]
        resources = ["*"]
      },
      {
        sid       = "WriteRunnerLogs"
        actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        resources = ["${aws_cloudwatch_log_group.runner.arn}:*"]
      },
      {
        sid     = "AssumeAnyWorkspaceRunRole"
        actions = ["sts:AssumeRole", "sts:TagSession"]
        resources = [
          "arn:aws:iam::*:role/${local.prefix}-workspace-*",
          "arn:aws:iam::*:role/${local.example_run_role_name}",
        ]
      },
    ],
    [
      for statement in concat(local.bucket_statements["State"], local.bucket_statements["Artifacts"]) : {
        sid       = statement.Sid
        actions   = statement.Action
        resources = statement.Resource
      }
    ],
  )
}

module "runner" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/ecs-fargate"
  version = "2.26.1"

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

      log_retention_days = 30

      task_policy_statements = local.runner_task_statements
    }
  }

  tags = { Component = "runner" }
}
