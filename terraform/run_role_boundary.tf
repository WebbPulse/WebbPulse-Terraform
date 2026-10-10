locals {
  run_role_boundary_name = "${local.prefix}-workspace-boundary"
  run_role_boundary_arn  = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:policy/${local.run_role_boundary_name}"

  plane_regional_services = [
    "cloudwatch",
    "dynamodb",
    "ecr",
    "ecs",
    "events",
    "kms",
    "lambda",
    "logs",
    "scheduler",
    "secretsmanager",
    "sns",
    "sqs",
    "ssm",
    "states",
  ]

  plane_resource_patterns = concat(
    [for service in local.plane_regional_services : "arn:aws:${service}:*:*:*${local.prefix}*"],
    [for kind in ["role", "policy"] : "arn:aws:iam::*:${kind}/*${local.prefix}*"],
    [
      "arn:aws:s3:::${local.prefix}-*",
      "arn:aws:s3:::${local.prefix}-*/*",
    ],
  )
}

data "aws_iam_policy_document" "run_role_boundary" {
  statement {
    sid       = "AllowWhatTheRolePolicyAllows"
    effect    = "Allow"
    actions   = ["*"]
    resources = ["*"]
  }

  statement {
    sid         = "DenyThePlaneByName"
    effect      = "Deny"
    not_actions = ["sts:AssumeRole"]
    resources   = local.plane_resource_patterns
  }

  statement {
    sid         = "DenyThePlaneByTag"
    effect      = "Deny"
    not_actions = ["sts:AssumeRole"]
    resources   = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Project"
      values   = [local.project]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Environment"
      values   = [var.environment]
    }
  }

  statement {
    sid    = "DenyDroppingABoundary"
    effect = "Deny"
    actions = [
      "iam:DeleteRolePermissionsBoundary",
      "iam:DeleteUserPermissionsBoundary",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "DenyUnboundedPrincipals"
    effect = "Deny"
    actions = [
      "iam:CreateRole",
      "iam:CreateUser",
      "iam:PutRolePermissionsBoundary",
      "iam:PutUserPermissionsBoundary",
    ]
    resources = ["*"]

    condition {
      test     = "StringNotEquals"
      variable = "iam:PermissionsBoundary"
      values   = [local.run_role_boundary_arn]
    }
  }
}

resource "aws_iam_policy" "run_role_boundary" {
  name        = local.run_role_boundary_name
  description = "Permissions boundary for every run role the plane can vend in its own account. Whatever policy such a role holds, it can never read or change the plane's own resources, found by name and by the plane's Project and Environment tags, and it can never create or free an unbounded principal."
  policy      = data.aws_iam_policy_document.run_role_boundary.json
}
