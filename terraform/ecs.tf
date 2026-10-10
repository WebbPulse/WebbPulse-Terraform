locals {
  runner_image = "${module.registry.repository_urls["runner"]}:${local.runner_image_tag}"

  workspace_run_role_arns = concat([
    "arn:aws:iam::*:role/${local.prefix}-workspace-*",
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${local.example_run_role_name}",
  ], var.external_run_role_arns)

  runner_task_statements = [
    {
      sid       = "WriteRunnerLogs"
      actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      resources = ["${aws_cloudwatch_log_group.runner.arn}:*"]
    },
  ]
}

module "runner" {
  source  = "terraform.webbpulse.com/WebbPulse/platform-modules/aws//modules/ecs-fargate"
  version = "~> 2.33"

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
