locals {
  e2e_run_role_count = local.ephemeral_users_enabled ? 1 : 0
  e2e_run_role_name  = "${local.prefix}-workspace-e2e"

  e2e_run_role_state_key_prefix = "workspaces/ws-*"
}

data "aws_iam_policy_document" "e2e_run_role_trust" {
  count = local.e2e_run_role_count

  statement {
    sid     = "RunnerTasksAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = values(module.runner.task_role_arns)
    }

    condition {
      test     = "StringLike"
      variable = "sts:ExternalId"
      values   = ["ws-*"]
    }
  }
}

data "aws_iam_policy_document" "e2e_run_role" {
  count = local.e2e_run_role_count

  statement {
    sid    = "StateObjectAndLock"
    effect = "Allow"

    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]

    resources = [
      "${module.state.bucket_arn}/${local.e2e_run_role_state_key_prefix}/terraform.tfstate",
      "${module.state.bucket_arn}/${local.e2e_run_role_state_key_prefix}/terraform.tfstate.tflock",
    ]
  }

  statement {
    sid    = "StateBucketList"
    effect = "Allow"

    actions = ["s3:ListBucket"]

    resources = [module.state.bucket_arn]
  }

  statement {
    sid    = "StateEncryption"
    effect = "Allow"

    actions = [
      "kms:Decrypt",
      "kms:Encrypt",
      "kms:GenerateDataKey",
      "kms:DescribeKey",
    ]

    resources = [module.state.kms_key_arn]
  }
}

resource "aws_iam_role" "e2e_run_role" {
  count = local.e2e_run_role_count

  name        = local.e2e_run_role_name
  description = "Run role the e2e suite passes when it creates a workspace, scoped to state objects under any ws- prefixed workspace"

  assume_role_policy = data.aws_iam_policy_document.e2e_run_role_trust[0].json

  tags = { Component = "e2e" }
}

resource "aws_iam_role_policy" "e2e_run_role" {
  count = local.e2e_run_role_count

  name   = "state"
  role   = aws_iam_role.e2e_run_role[0].id
  policy = data.aws_iam_policy_document.e2e_run_role[0].json
}
