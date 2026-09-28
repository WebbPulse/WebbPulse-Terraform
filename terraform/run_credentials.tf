variable "run_credentials_duration_seconds" {
  description = "Seconds each vended run role and state session lasts. Chained AssumeRole caps it at an hour, and the runner refreshes sessions before they expire, so a lower value only shortens how long a leaked key works"
  type        = number
  default     = 3600

  validation {
    condition     = var.run_credentials_duration_seconds >= 900 && var.run_credentials_duration_seconds <= 3600
    error_message = "run_credentials_duration_seconds must be between 900 and 3600, the range STS accepts for a chained role session."
  }
}

variable "external_run_role_arns" {
  description = "Exact ARNs of run roles outside the <prefix>-workspace-* naming that the vending role may also assume, such as the WebbPulse-Platform factory's <Workspace>-Terraform roles. Each role still has to trust the vending role with the workspace id as its external id. Wildcards are refused so the grant never widens past the roles named here."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for arn in var.external_run_role_arns : can(regex("^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$", arn))
    ])
    error_message = "external_run_role_arns entries must be exact IAM role ARNs of the form arn:aws:iam::<12 digit account id>:role/<name>, with no wildcards."
  }

  validation {
    condition     = length(distinct(var.external_run_role_arns)) == length(var.external_run_role_arns)
    error_message = "external_run_role_arns must not repeat an ARN."
  }
}

locals {
  runs_lambda_role_arn        = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${local.prefix}-runs-lambda"
  run_credentials_role_name   = "${local.prefix}-run-credentials"
  run_credentials_role_arn    = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${local.run_credentials_role_name}"
  run_state_role_name         = "${local.prefix}-run-state"
  run_state_role_arn          = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${local.run_state_role_name}"
  run_credentials_session_ttl = 3600
}

data "aws_iam_policy_document" "run_credentials_trust" {
  statement {
    sid     = "OnlyTheRunsFunction"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:PrincipalArn"
      values   = [local.runs_lambda_role_arn]
    }
  }
}

data "aws_iam_policy_document" "run_credentials" {
  statement {
    sid       = "AssumeWorkspaceRunRoles"
    effect    = "Allow"
    actions   = ["sts:AssumeRole"]
    resources = local.workspace_run_role_arns
  }

  statement {
    sid       = "AssumeTheStateRole"
    effect    = "Allow"
    actions   = ["sts:AssumeRole"]
    resources = [local.run_state_role_arn]
  }
}

resource "aws_iam_role" "run_credentials" {
  name                 = local.run_credentials_role_name
  description          = "The only principal workspace run roles trust. The runs function assumes it to vend each run phase its credentials, so no runner task holds a path to a workspace role."
  max_session_duration = local.run_credentials_session_ttl

  assume_role_policy = data.aws_iam_policy_document.run_credentials_trust.json

  tags = { Component = "runner" }
}

resource "aws_iam_role_policy" "run_credentials" {
  name   = "vend"
  role   = aws_iam_role.run_credentials.id
  policy = data.aws_iam_policy_document.run_credentials.json
}

data "aws_iam_policy_document" "run_state_trust" {
  statement {
    sid     = "OnlyTheVendingRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:PrincipalArn"
      values   = [local.run_credentials_role_arn]
    }
  }
}

resource "aws_iam_role" "run_state" {
  name                 = local.run_state_role_name
  description          = "State bucket access for run phases. Always assumed with a session policy that narrows it to one workspace's state prefix."
  max_session_duration = local.run_credentials_session_ttl

  assume_role_policy = data.aws_iam_policy_document.run_state_trust.json

  tags = { Component = "runner" }
}

resource "aws_iam_role_policy" "run_state" {
  name = "state"
  role = aws_iam_role.run_state.id

  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = local.bucket_statements["State"]
  })
}
