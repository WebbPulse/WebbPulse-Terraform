locals {
  e2e_quick_setup_role_name = "${local.prefix}-e2e-quick-setup"
  e2e_quick_setup_ulid      = "??????????????????????????"

  e2e_quick_setup_stack_arn = "arn:aws:cloudformation:${var.aws_region}:${data.aws_caller_identity.current.account_id}:stack/${local.prefix}-workspace-${local.e2e_quick_setup_ulid}/*"

  e2e_quick_setup_role_arns = [
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${local.prefix}-workspace-${local.e2e_quick_setup_ulid}",
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/*plan-${local.e2e_quick_setup_ulid}",
  ]

  e2e_quick_setup_policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

data "aws_iam_policy_document" "e2e_quick_setup_trust" {
  count = local.e2e_run_role_count

  statement {
    sid     = "TheE2EJobAssumes"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${local.prefix}-github-actions-deploy"]
    }
  }
}

data "aws_iam_policy_document" "e2e_quick_setup" {
  count = local.e2e_run_role_count

  statement {
    sid    = "QuickSetupStacks"
    effect = "Allow"
    actions = [
      "cloudformation:CreateStack",
      "cloudformation:DeleteStack",
      "cloudformation:DescribeStacks",
      "cloudformation:DescribeStackEvents",
      "cloudformation:DescribeStackResources",
    ]
    resources = [local.e2e_quick_setup_stack_arn]
  }

  statement {
    sid       = "CreateOnlyBoundedRoles"
    effect    = "Allow"
    actions   = ["iam:CreateRole", "iam:PutRolePermissionsBoundary"]
    resources = local.e2e_quick_setup_role_arns

    condition {
      test     = "StringEquals"
      variable = "iam:PermissionsBoundary"
      values   = [aws_iam_policy.run_role_boundary.arn]
    }
  }

  statement {
    sid       = "AttachOnlyReadOnlyAccess"
    effect    = "Allow"
    actions   = ["iam:AttachRolePolicy", "iam:DetachRolePolicy"]
    resources = local.e2e_quick_setup_role_arns

    condition {
      test     = "ArnEquals"
      variable = "iam:PolicyARN"
      values   = [local.e2e_quick_setup_policy_arn]
    }
  }

  statement {
    sid    = "ManageQuickSetupRoles"
    effect = "Allow"
    actions = [
      "iam:DeleteRole",
      "iam:DeleteRolePolicy",
      "iam:GetRolePolicy",
      "iam:ListAttachedRolePolicies",
      "iam:ListInstanceProfilesForRole",
      "iam:ListRolePolicies",
      "iam:ListRoleTags",
      "iam:PutRolePolicy",
      "iam:TagRole",
      "iam:UntagRole",
      "iam:UpdateRole",
      "iam:UpdateRoleDescription",
    ]
    resources = local.e2e_quick_setup_role_arns
  }

  statement {
    sid       = "InspectWorkspaceRoles"
    effect    = "Allow"
    actions   = ["iam:GetRole", "iam:SimulatePrincipalPolicy"]
    resources = concat(["arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${local.prefix}-workspace-*"], local.e2e_quick_setup_role_arns)
  }

  statement {
    sid       = "ReadTheBoundary"
    effect    = "Allow"
    actions   = ["iam:GetPolicy", "iam:GetPolicyVersion"]
    resources = [aws_iam_policy.run_role_boundary.arn]
  }

  statement {
    sid       = "SimulateTheBoundary"
    effect    = "Allow"
    actions   = ["iam:SimulateCustomPolicy"]
    resources = ["*"]
  }

  statement {
    sid       = "ReportThroughCloudFormation"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.aws_connect.arn]

    condition {
      test     = "ForAnyValue:StringEquals"
      variable = "aws:CalledVia"
      values   = ["cloudformation.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "e2e_quick_setup" {
  count = local.e2e_run_role_count

  name        = local.e2e_quick_setup_role_name
  description = "Assumed by the staging e2e job to create and delete Quick setup stacks in this account. It can only create workspace roles that carry the run role boundary and only attach ReadOnlyAccess, and no workspace can vend it because its name is outside the vending grant."

  assume_role_policy = data.aws_iam_policy_document.e2e_quick_setup_trust[0].json

  tags = { Component = "e2e" }
}

resource "aws_iam_role_policy" "e2e_quick_setup" {
  count = local.e2e_run_role_count

  name   = "quick-setup-stacks"
  role   = aws_iam_role.e2e_quick_setup[0].id
  policy = data.aws_iam_policy_document.e2e_quick_setup[0].json
}
