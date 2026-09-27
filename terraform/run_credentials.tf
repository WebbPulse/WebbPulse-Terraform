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
