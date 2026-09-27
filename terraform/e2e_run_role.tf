locals {
  e2e_run_role_count = local.ephemeral_users_enabled ? 1 : 0
  e2e_run_role_name  = "${local.prefix}-workspace-e2e"
}

data "aws_iam_policy_document" "e2e_run_role_trust" {
  count = local.e2e_run_role_count

  statement {
    sid     = "RunCredentialsVendingAssumes"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.run_credentials.arn]
    }

    condition {
      test     = "StringLike"
      variable = "sts:ExternalId"
      values   = ["ws-*"]
    }
  }
}

resource "aws_iam_role" "e2e_run_role" {
  count = local.e2e_run_role_count

  name        = local.e2e_run_role_name
  description = "The only apply boundary the e2e suite ever uses. It holds no permissions: the suite's applies are random_pet only, and state goes through the control plane's per run state credentials, so nothing billable can be created through it."

  assume_role_policy = data.aws_iam_policy_document.e2e_run_role_trust[0].json

  tags = { Component = "e2e" }
}
