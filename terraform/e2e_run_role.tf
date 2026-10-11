locals {
  e2e_run_role_count = local.ephemeral_users_enabled ? 1 : 0
  e2e_run_role_name  = "${local.prefix}-workspace-e2e"
  e2e_project_id     = "prj-01M4MMMBY4YNCV5C3WNBEBQJQD"
  e2e_project_name   = "e2e"
  workspace_id_glob  = "ws-??????????????????????????"
}

resource "aws_dynamodb_table_item" "e2e_project" {
  count = local.e2e_run_role_count

  table_name = module.dynamodb.table_names["projects"]
  hash_key   = "project_id"

  item = jsonencode({
    project_id  = { S = local.e2e_project_id }
    name        = { S = local.e2e_project_name }
    name_key    = { S = local.e2e_project_name }
    description = { S = "Workspaces the e2e suite creates. The e2e run role trusts only sessions tagged with this project." }
    created_at  = { S = "2026-10-11T05:00:00Z" }
    updated_at  = { S = "2026-10-11T05:00:00Z" }
  })
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
      values   = [local.workspace_id_glob]
    }

    condition {
      test     = "StringLike"
      variable = "sts:RoleSessionName"
      values   = ["run-*@e2e-*"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/project"
      values   = [local.e2e_project_id]
    }

    condition {
      test     = "StringLike"
      variable = "aws:RequestTag/workspace"
      values   = [local.workspace_id_glob]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/run_phase"
      values   = ["plan", "apply"]
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

      condition {
        test     = "StringEquals"
        variable = "aws:RequestTag/workspace"
        values   = var.e2e_run_role_workspace_ids
      }

      condition {
        test     = "StringEquals"
        variable = "aws:RequestTag/run_phase"
        values   = ["plan", "apply"]
      }
    }
  }

  statement {
    sid     = "TagE2ESuiteSessions"
    effect  = "Allow"
    actions = ["sts:TagSession"]

    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.run_credentials.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/project"
      values   = [local.e2e_project_id]
    }

    condition {
      test     = "StringLike"
      variable = "aws:RequestTag/workspace"
      values   = [local.workspace_id_glob]
    }
  }

  dynamic "statement" {
    for_each = length(var.e2e_run_role_workspace_ids) > 0 ? [1] : []

    content {
      sid     = "TagDurableE2ESessions"
      effect  = "Allow"
      actions = ["sts:TagSession"]

      principals {
        type        = "AWS"
        identifiers = [aws_iam_role.run_credentials.arn]
      }

      condition {
        test     = "StringEquals"
        variable = "aws:RequestTag/workspace"
        values   = var.e2e_run_role_workspace_ids
      }
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
