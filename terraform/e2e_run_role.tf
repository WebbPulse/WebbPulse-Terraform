locals {
  e2e_run_role_count = local.ephemeral_users_enabled ? 1 : 0
  e2e_run_role_name  = "${local.prefix}-workspace-e2e"
}

data "aws_iam_policy_document" "e2e_run_role_trust" {
  count = local.e2e_run_role_count

  statement {
    sid     = "E2ESuiteWorkspaces"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.run_credentials.arn]
    }

    condition {
      test     = "StringLike"
      variable = "sts:ExternalId"
      values   = ["ws-??????????????????????????"]
    }

    condition {
      test     = "StringLike"
      variable = "sts:RoleSessionName"
      values   = ["run-*@e2e-*"]
    }
  }

  dynamic "statement" {
    for_each = length(var.e2e_run_role_workspace_ids) > 0 ? [1] : []

    content {
      sid     = "DurableE2EWorkspaces"
      effect  = "Allow"
      actions = ["sts:AssumeRole"]

      principals {
        type        = "AWS"
        identifiers = [aws_iam_role.run_credentials.arn]
      }

      condition {
        test     = "StringEquals"
        variable = "sts:ExternalId"
        values   = var.e2e_run_role_workspace_ids
      }
    }
  }

  statement {
    sid     = "TagWorkspaceSessions"
    effect  = "Allow"
    actions = ["sts:TagSession"]

    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.run_credentials.arn]
    }

    condition {
      test     = "StringLike"
      variable = "aws:RequestTag/workspace"
      values   = ["ws-??????????????????????????"]
    }
  }
}

resource "aws_iam_role" "e2e_run_role" {
  count = local.e2e_run_role_count

  name        = local.e2e_run_role_name
  description = "The only apply boundary the e2e suite ever uses. It holds no permissions beyond assuming the two permissionless e2e plan reader roles: the suite's applies are random_pet only, and state goes through the control plane's per run state credentials, so nothing billable can be created through it."

  assume_role_policy   = data.aws_iam_policy_document.e2e_run_role_trust[0].json
  permissions_boundary = aws_iam_policy.run_role_boundary.arn

  tags = { Component = "e2e" }
}

locals {
  e2e_plan_reader_role_names = {
    listed   = "${local.prefix}-plan-reader-e2e"
    unlisted = "${local.prefix}-plan-unlisted-e2e"
  }
}

data "aws_iam_policy_document" "e2e_plan_reader_trust" {
  count = local.e2e_run_role_count

  statement {
    sid     = "E2ERunRoleAssumes"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.e2e_run_role[0].arn]
    }
  }
}

resource "aws_iam_role" "e2e_plan_reader" {
  for_each = local.e2e_run_role_count == 1 ? local.e2e_plan_reader_role_names : {}

  name        = each.value
  description = "Permissionless target for the e2e plan_assume_role_arns proof. The e2e suite lists the listed role on a workspace and not the unlisted one, so the plan session policy is the only difference between an assume that succeeds and one that is denied."

  assume_role_policy   = data.aws_iam_policy_document.e2e_plan_reader_trust[0].json
  permissions_boundary = aws_iam_policy.run_role_boundary.arn

  tags = { Component = "e2e" }
}

data "aws_iam_policy_document" "e2e_run_role_assumes_readers" {
  count = local.e2e_run_role_count

  statement {
    sid       = "AssumeE2EPlanReaders"
    effect    = "Allow"
    actions   = ["sts:AssumeRole"]
    resources = [for role in aws_iam_role.e2e_plan_reader : role.arn]
  }
}

resource "aws_iam_role_policy" "e2e_run_role_assumes_readers" {
  count = local.e2e_run_role_count

  name   = "assume-e2e-plan-readers"
  role   = aws_iam_role.e2e_run_role[0].id
  policy = data.aws_iam_policy_document.e2e_run_role_assumes_readers[0].json
}
