locals {
  lambda_domains_declared = {
    workspaces = {
      memory            = 512
      tables            = ["workspaces", "variables", "config-versions", "users"]
      read_tables       = ["runs"]
      sqs_event_sources = {}
    }
    runs = {
      memory      = 512
      tables      = ["runs"]
      read_tables = ["workspaces", "variables", "config-versions"]
      sqs_event_sources = {
        run_confirmations = {
          queue_arn                       = module.run_confirmations.queue_arn
          batch_size                      = 1
          maximum_batching_window_seconds = 0
        }
      }
    }
  }

  domain_functions_enabled = var.bootstrap_image_tag != ""

  lambda_domains = local.domain_functions_enabled ? local.lambda_domains_declared : {}

  dynamodb_write_actions = [
    "dynamodb:GetItem",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
    "dynamodb:Query",
    "dynamodb:Scan",
    "dynamodb:BatchGetItem",
    "dynamodb:BatchWriteItem",
    "dynamodb:TransactWriteItems",
    "dynamodb:TransactGetItems",
    "dynamodb:DescribeTable",
    "dynamodb:ConditionCheckItem",
  ]

  dynamodb_read_actions = [
    "dynamodb:GetItem",
    "dynamodb:Query",
    "dynamodb:Scan",
    "dynamodb:BatchGetItem",
    "dynamodb:DescribeTable",
  ]

  lambda_domain_write_arns = {
    for name, domain in local.lambda_domains : name => flatten([
      for table in domain.tables : [
        module.dynamodb.table_arns[table],
        "${module.dynamodb.table_arns[table]}/index/*",
      ]
    ])
  }

  bucket_statements = {
    for label, statements in {
      State     = module.state.read_write_policy_statements
      Artifacts = module.artifacts.read_write_policy_statements
      } : label => [
      for statement in statements : merge(statement, { Sid = "${label}Bucket${statement.Sid}" })
    ]
  }

  lambda_domain_read_arns = {
    for name, domain in local.lambda_domains : name => flatten([
      for table in domain.read_tables : [
        module.dynamodb.table_arns[table],
        "${module.dynamodb.table_arns[table]}/index/*",
      ]
    ])
  }
}

module "lambda_domain" {
  for_each = local.lambda_domains

  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/lambda-function"
  version = "~> 2.27"

  function_name = "${local.prefix}-${each.key}"
  role_name     = "${local.prefix}-${each.key}-lambda"

  package_type = "Image"

  architectures = ["arm64"]
  memory_size   = each.value.memory

  timeout = local.lambda_domain_timeout

  sqs_event_sources = each.value.sqs_event_sources

  code = {
    image_uri = "${module.registry.repository_urls[each.key]}:${var.bootstrap_image_tag}"
  }

  environment_variables = merge(
    {
      ENVIRONMENT  = var.environment
      SERVICE_NAME = "${local.project}-${each.key}"
      CORS_ORIGINS = local.cors_origins
      SITE_URL     = local.frontend_url
      LOG_LEVEL    = "INFO"

      WORKSPACES_TABLE      = module.dynamodb.table_names["workspaces"]
      RUNS_TABLE            = module.dynamodb.table_names["runs"]
      VARIABLES_TABLE       = module.dynamodb.table_names["variables"]
      CONFIG_VERSIONS_TABLE = module.dynamodb.table_names["config-versions"]
      USERS_TABLE           = module.dynamodb.table_names["users"]

      IDENTITY_TABLE_PREFIX = local.prefix

      STATE_BUCKET     = module.state.bucket
      ARTIFACTS_BUCKET = module.artifacts.bucket
      RUNNER_LOG_GROUP = aws_cloudwatch_log_group.runner.name

      RUN_STATE_MACHINE_ARN = module.run_state_machine.arn

      RUNNER_TASK_ROLE_ARN = join(",", sort(values(module.runner.task_role_arns)))
      RUN_ROLE_NAME_PREFIX = "${local.prefix}-workspace-"

      APP_SECRETS_ARN = module.app_secrets.arns["app"]

      WEBBPULSE_OTEL_SAMPLE_RATIO        = var.environment == "production" ? "0.1" : "1.0"
      OTEL_EXPORTER_OTLP_TRACES_ENDPOINT = "https://xray.${var.aws_region}.amazonaws.com/v1/traces"
    },

    {
      IDENTITY_ENVIRONMENT       = var.environment
      IDENTITY_RP_NAME           = var.identity_rp_name
      IDENTITY_PRODUCT_NAME      = "WebbPulse Terraform"
      IDENTITY_SUPPORT_EMAIL     = "tyler@webbpulse.com"
      IDENTITY_FRONTEND_BASE_URL = "https://${local.host}"

      IDENTITY_REGISTRATION_ENABLED = "false"

      IDENTITY_TOTP_CIPHER = "secret"

      IDENTITY_OAUTH_REDIRECT_URIS = local.identity_oauth_redirect_uris

      IDENTITY_EPHEMERAL_USERS_ENABLED = tostring(local.ephemeral_users_enabled)

      IDENTITY_PASSKEYS_ENABLED      = tostring(local.passkeys_enabled)
      IDENTITY_PASSKEYS_PASSWORDLESS = tostring(local.passkeys_passwordless)
      IDENTITY_WEBAUTHN_ORIGINS      = local.identity_webauthn_origins
    },

    module.identity.identity_environment,
  )

  log_retention_days           = 7
  log_format                   = "JSON"
  application_log_level        = "INFO"
  system_log_level             = "INFO"
  set_logging_config_log_group = true

  tracing_mode             = "Active"
  attach_xray_write_policy = true
}

locals {
  lambda_domain_extra_statements = {
    workspaces = [
      {
        Sid      = "CheckAWorkspaceRunRole"
        Effect   = "Allow"
        Action   = ["sts:AssumeRole"]
        Resource = local.workspace_run_role_arns
      },
    ]
    runs = [
      {
        Sid      = "StartAndStopRunExecutions"
        Effect   = "Allow"
        Action   = ["states:StartExecution", "states:StopExecution", "states:DescribeExecution"]
        Resource = [module.run_state_machine.arn, "${replace(module.run_state_machine.arn, ":stateMachine:", ":execution:")}:*"]
      },
      {
        Sid      = "ResumeAConfirmedRun"
        Effect   = "Allow"
        Action   = ["states:SendTaskSuccess", "states:SendTaskFailure"]
        Resource = ["*"]
      },
      {
        Sid      = "ReadRunnerLogs"
        Effect   = "Allow"
        Action   = ["logs:GetLogEvents", "logs:DescribeLogStreams"]
        Resource = ["${aws_cloudwatch_log_group.runner.arn}:*"]
      },
    ]
  }
}

resource "aws_iam_role_policy" "lambda_domain" {
  for_each = local.lambda_domains

  name = "${each.key}-runtime"
  role = module.lambda_domain[each.key].role_id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          Sid      = "WriteOwnLogs"
          Effect   = "Allow"
          Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
          Resource = "${module.lambda_domain[each.key].log_group_arn}:*"
        },
        {
          Sid      = "WriteSpansToTheXRayOTLPEndpoint"
          Effect   = "Allow"
          Action   = ["xray:PutSpans", "xray:PutSpansForIndexing"]
          Resource = "*"
        },
        {
          Sid      = "ReadWriteOwnTables"
          Effect   = "Allow"
          Action   = local.dynamodb_write_actions
          Resource = local.lambda_domain_write_arns[each.key]
        },
        {
          Sid      = "ReadSharedTables"
          Effect   = "Allow"
          Action   = local.dynamodb_read_actions
          Resource = local.lambda_domain_read_arns[each.key]
        },
        module.app_secrets.read_policy_statement,
      ],
      local.bucket_statements["State"],
      local.bucket_statements["Artifacts"],
      local.lambda_domain_extra_statements[each.key],
    )
  })
}
