variable "plan_timeout_seconds" {
  description = "Seconds the plan phase may run before Step Functions times the state out and marks the run errored"
  type        = number
  default     = 1800
}

variable "apply_timeout_seconds" {
  description = "Seconds the apply phase may run before Step Functions times the state out and marks the run errored"
  type        = number
  default     = 7200
}

variable "confirmation_timeout_seconds" {
  description = "Seconds a planned run waits for a confirmation before the execution gives up and marks it errored"
  type        = number
  default     = 86400
}

variable "run_concurrency_cap" {
  description = "Concurrent runner tasks allowed in this environment, held as the size of the semaphore item's holders set on the runs table"
  type        = number
  default     = 2
}

module "run_state_machine" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/step-functions"
  version = "2.24.0"

  name = "${local.prefix}-run"

  definition = templatefile("${path.module}/state_machines/run.asl.json", {
    RunsTable    = module.dynamodb.table_names["runs"]
    SemaphoreCap = tostring(var.run_concurrency_cap)
    ClusterArn   = module.runner.cluster_arn
    ApiBaseUrl   = local.api_url

    PlanTaskDefinitionArn  = module.runner.task_definition_family_arns["plan"]
    ApplyTaskDefinitionArn = module.runner.task_definition_family_arns["apply"]
    PlanContainerName      = module.runner.container_names["plan"]
    ApplyContainerName     = module.runner.container_names["apply"]

    SubnetIdsJson        = jsonencode(module.vpc.public_subnet_ids)
    SecurityGroupIdsJson = jsonencode([module.vpc.task_security_group_id])

    PlanTimeoutSeconds         = tostring(var.plan_timeout_seconds)
    ApplyTimeoutSeconds        = tostring(var.apply_timeout_seconds)
    ConfirmationTimeoutSeconds = tostring(var.confirmation_timeout_seconds)
  })

  policy_statements = [
    {
      sid       = "RunAPhaseTask"
      actions   = ["ecs:RunTask"]
      resources = [for phase in ["plan", "apply"] : "${module.runner.task_definition_family_arns[phase]}:*"]
      condition = {
        ArnEquals = { "ecs:cluster" = [module.runner.cluster_arn] }
      }
    },
    {
      sid       = "DescribeAndStopAPhaseTask"
      actions   = ["ecs:DescribeTasks", "ecs:StopTask"]
      resources = ["*"]
      condition = {
        ArnEquals = { "ecs:cluster" = [module.runner.cluster_arn] }
      }
    },
    {
      sid       = "PassTheRunnerRolesToEcs"
      actions   = ["iam:PassRole"]
      resources = sort(distinct(concat([module.runner.execution_role_arn], values(module.runner.task_role_arns))))
      condition = {
        StringEquals = { "iam:PassedToService" = ["ecs-tasks.amazonaws.com"] }
      }
    },
    {
      sid       = "MarkRunsAndHoldTheSemaphore"
      actions   = ["dynamodb:UpdateItem", "dynamodb:GetItem"]
      resources = [module.dynamodb.table_arns["runs"]]
    },
  ]

  log_retention_days     = 30
  include_execution_data = false

  tracing_enabled = true
}
