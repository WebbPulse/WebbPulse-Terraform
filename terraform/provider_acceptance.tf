module "provider_acceptance_role" {
  count = local.staging_gate_count

  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/github-actions-role"
  version = "~> 2.27"

  role_name        = "${local.prefix}-provider-acceptance"
  role_description = "Reads the staging gate header and mints the admin token that creates an ephemeral user for provider acceptance tests. Assumable only from the staging-acceptance environment of WebbPulse/terraform-provider-webbpulse."

  create_oidc_provider = false
  oidc_provider_arn    = module.github_actions_role.oidc_provider_arn

  subjects = ["${local.provider_repo_subject_prefix}:environment:staging-acceptance"]

  inline_policy_name = "provider-acceptance"

  policy_statements = [
    {
      sid       = "ReadGateHeader"
      actions   = ["ssm:GetParameter"]
      resources = [one(module.staging_access_gate[*].origin_verify_ssm_parameter_arn)]
    },
    {
      sid       = "DecryptGateHeader"
      actions   = ["kms:Decrypt"]
      resources = [data.aws_kms_alias.ssm.target_key_arn]
      condition = {
        StringEquals = { "kms:ViaService" = ["ssm.${var.aws_region}.amazonaws.com"] }
      }
    },
    {
      sid       = "MintStagingIdentityToken"
      actions   = ["kms:Sign", "kms:GetPublicKey"]
      resources = module.identity.signing_key_arns
    },
  ]
}
