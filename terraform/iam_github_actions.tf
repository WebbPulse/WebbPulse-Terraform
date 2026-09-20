data "aws_kms_alias" "ssm" {
  name = "alias/aws/ssm"
}

locals {
  github_org_id  = "185014056"
  github_repo_id = "1375403660"

  github_subject_prefix = "repo:WebbPulse@${local.github_org_id}/WebbPulse-Terraform@${local.github_repo_id}"

  artifacts_account_id = "432410731887"

  codeartifact_domain_arn = "arn:aws:codeartifact:${var.aws_region}:${local.artifacts_account_id}:domain/webbpulse"

  codeartifact_repository_arns = [
    for name in ["npm", "npm-store", "pypi-store", "python", "shared"] :
    "arn:aws:codeartifact:${var.aws_region}:${local.artifacts_account_id}:repository/webbpulse/${name}"
  ]

  codeartifact_package_arns = [
    for name in ["npm", "npm-store", "pypi-store", "python", "shared"] :
    "arn:aws:codeartifact:${var.aws_region}:${local.artifacts_account_id}:package/webbpulse/${name}/*"
  ]

  shared_base_image_repository_arn = "arn:aws:ecr:${var.aws_region}:${local.artifacts_account_id}:repository/webbpulse/python-lambda-base"

  lambda_domain_function_arns = [
    for name in sort(keys(local.lambda_domains_declared)) :
    "arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:${local.prefix}-${name}"
  ]

  github_actions_codeartifact_statements = [
    {
      sid       = "CodeArtifactToken"
      actions   = ["codeartifact:GetAuthorizationToken"]
      resources = [local.codeartifact_domain_arn]
    },
    {
      sid = "CodeArtifactRead"
      actions = [
        "codeartifact:DescribePackageVersion",
        "codeartifact:DescribeRepository",
        "codeartifact:GetPackageVersionAsset",
        "codeartifact:GetPackageVersionReadme",
        "codeartifact:GetRepositoryEndpoint",
        "codeartifact:ListPackageVersionAssets",
        "codeartifact:ListPackageVersionDependencies",
        "codeartifact:ListPackageVersions",
        "codeartifact:ListPackages",
        "codeartifact:ReadFromRepository",
      ]
      resources = concat(local.codeartifact_repository_arns, local.codeartifact_package_arns)
    },
    {
      sid       = "CodeArtifactBearerToken"
      actions   = ["sts:GetServiceBearerToken"]
      resources = ["*"]
      condition = {
        StringEquals = {
          "sts:AWSServiceName" = ["codeartifact.amazonaws.com"]
        }
      }
    },
  ]

  github_actions_gate_statements = [for statement in [
    {
      sid     = "ReadGateParameters"
      actions = ["ssm:GetParameter"]
      resources = [
        one(module.staging_access_gate[*].origin_verify_ssm_parameter_arn),
        one(module.staging_access_gate[*].signing_key_ssm_parameter_arn),
      ]
    },
    {
      sid       = "DecryptGateParameters"
      actions   = ["kms:Decrypt"]
      resources = [data.aws_kms_alias.ssm.target_key_arn]
      condition = {
        StringEquals = { "kms:ViaService" = ["ssm.${var.aws_region}.amazonaws.com"] }
      }
    },
  ] : statement if local.staging_gate_enabled]

  github_actions_e2e_statements = concat([
    {
      sid       = "E2EReadGatewayRoutes"
      actions   = ["apigateway:GET"]
      resources = [module.api.api_arn, "${module.api.api_arn}/*"]
    },
    {
      sid       = "E2EReadAccessLog"
      actions   = ["logs:FilterLogEvents"]
      resources = [module.api.access_log_group_arn, "${module.api.access_log_group_arn}:*"]
    },
    ], var.environment == "staging" ? [
    {
      sid       = "E2EMintStagingIdentityToken"
      actions   = ["kms:Sign", "kms:GetPublicKey"]
      resources = module.identity.signing_key_arns
    },
  ] : [])
}

module "github_actions_role" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/github-actions-role"
  version = "~> 2.27"

  role_name = "${local.prefix}-github-actions-deploy"

  subjects = [
    "${local.github_subject_prefix}:environment:staging",
    "${local.github_subject_prefix}:environment:production",
  ]

  policy_statements = concat([
    {
      sid = "UpdateDomainFunctions"
      actions = [
        "lambda:UpdateFunctionCode",
        "lambda:GetFunction",
        "lambda:GetFunctionConfiguration",
        "lambda:PublishVersion",
      ]
      resources = local.lambda_domain_function_arns
    },
    {
      sid       = "SmokeInvokeDomainFunctions"
      actions   = ["lambda:InvokeFunction"]
      resources = local.lambda_domain_function_arns
    },
    {
      sid = "PublishFrontendAssets"
      actions = [
        "s3:PutObject",
        "s3:GetObject",
        "s3:DeleteObject",
        "s3:ListBucket",
      ]
      resources = [
        module.frontend.bucket_arn,
        "${module.frontend.bucket_arn}/*",
      ]
    },
    {
      sid = "InvalidateFrontendCache"
      actions = [
        "cloudfront:CreateInvalidation",
        "cloudfront:GetInvalidation",
      ]
      resources = [module.frontend.distribution_arn]
    },
    {
      sid       = "EcrAuth"
      actions   = ["ecr:GetAuthorizationToken"]
      resources = ["*"]
    },
    {
      sid = "EcrPushDomainAndRunnerImages"
      actions = [
        "ecr:BatchCheckLayerAvailability",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:PutImage",
        "ecr:BatchGetImage",
        "ecr:DescribeImages",
        "ecr:GetDownloadUrlForLayer",
        "ecr:GetRepositoryPolicy",
        "ecr:SetRepositoryPolicy",
      ]
      resources = module.registry.repository_arns_list
    },
    {
      sid = "SharedBaseImagePull"
      actions = [
        "ecr:BatchGetImage",
        "ecr:DescribeImages",
        "ecr:GetDownloadUrlForLayer",
      ]
      resources = [local.shared_base_image_repository_arn]
    },
  ], local.github_actions_codeartifact_statements, local.github_actions_gate_statements, local.github_actions_e2e_statements)
}

module "github_actions_ci_role" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/github-actions-role"
  version = "~> 2.27"

  role_name        = "${local.prefix}-github-actions-ci"
  role_description = "Read-only CodeArtifact access for pull request CI in WebbPulse/WebbPulse-Terraform. Deploy permissions live on the separate github-actions-deploy role."

  create_oidc_provider = false
  oidc_provider_arn    = module.github_actions_role.oidc_provider_arn

  subjects = [
    "${local.github_subject_prefix}:pull_request",
    "${local.github_subject_prefix}:ref:refs/heads/staging",
    "${local.github_subject_prefix}:ref:refs/heads/main",
  ]

  inline_policy_name = "codeartifact-read"

  policy_statements = local.github_actions_codeartifact_statements
}
