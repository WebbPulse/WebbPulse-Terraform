locals {
  example_run_role_count = var.example_workspace_id == "" ? 0 : 1
  example_run_role_name  = "${local.prefix}-example-run-role"

  example_run_role_state_key = "workspaces/${var.example_workspace_id}/terraform.tfstate"
}

data "aws_iam_policy_document" "example_run_role_trust" {
  count = local.example_run_role_count

  statement {
    sid     = "RunnerTasksAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = values(module.runner.task_role_arns)
    }

    condition {
      test     = "StringEquals"
      variable = "sts:ExternalId"
      values   = [var.example_workspace_id]
    }
  }
}

data "aws_iam_policy_document" "example_run_role" {
  count = local.example_run_role_count

  statement {
    sid    = "StateObjectAndLock"
    effect = "Allow"

    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]

    resources = [
      "${module.state.bucket_arn}/${local.example_run_role_state_key}",
      "${module.state.bucket_arn}/${local.example_run_role_state_key}.tflock",
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

resource "aws_iam_role" "example_run_role" {
  count = local.example_run_role_count

  name        = local.example_run_role_name
  description = "Run role for the first end to end staging run, scoped to the example workspace's state object"

  assume_role_policy = data.aws_iam_policy_document.example_run_role_trust[0].json

  tags = { Component = "example-run" }
}

resource "aws_iam_role_policy" "example_run_role" {
  count = local.example_run_role_count

  name   = "state"
  role   = aws_iam_role.example_run_role[0].id
  policy = data.aws_iam_policy_document.example_run_role[0].json
}
