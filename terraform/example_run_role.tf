locals {
  example_run_role_count = var.example_workspace_id == "" ? 0 : 1
  example_run_role_name  = "${local.prefix}-example-run-role"
}

data "aws_iam_policy_document" "example_run_role_trust" {
  count = local.example_run_role_count

  statement {
    sid     = "RunCredentialsVendingAssumes"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.run_credentials.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "sts:ExternalId"
      values   = [var.example_workspace_id]
    }
  }
}

resource "aws_iam_role" "example_run_role" {
  count = local.example_run_role_count

  name        = local.example_run_role_name
  description = "Run role for the first end to end staging run. It holds no permissions; state goes through the per run state credentials"

  assume_role_policy = data.aws_iam_policy_document.example_run_role_trust[0].json

  tags = { Component = "example-run" }
}
