locals {
  project = "webbpulse-terraform"

  lambda_domain_timeout = 30

  env_slug = var.environment == "production" ? "prod" : "staging"

  prefix = "${local.project}-${local.env_slug}"

  common_tags = {
    Project     = local.project
    Environment = var.environment
    ManagedBy   = "terraform"
  }

  custom_domains_enabled = var.staging_profile == "full" && var.route53_zone_id != null
  custom_domain_count    = local.custom_domains_enabled ? 1 : 0

  parent_domain = "webbpulse.com"

  host     = var.environment == "production" ? "terraform.${local.parent_domain}" : "staging.terraform.${local.parent_domain}"
  api_host = "api.${local.host}"

  workload_dns_role_arn = var.environment == "production" ? var.route53_write_role_arn : ""
  records_zone_id       = var.environment == "production" ? var.route53_zone_id : module.staging_dns.zone_id

  frontend_url = module.frontend.frontend_url
  api_url      = module.api.api_url

  cors_origins = local.custom_domains_enabled ? "https://${local.host}" : local.frontend_url

  access_gate_requested = var.access_gate || var.staging_access_gate
  access_gate_enabled   = local.access_gate_requested && local.custom_domains_enabled
  access_gate_count     = local.access_gate_enabled ? 1 : 0
  access_gate_name      = var.environment == "production" ? local.prefix : "${local.project}-stg"
  access_gate_users     = distinct(concat(var.staging_access_users, var.access_gate_users))

  access_gate_authorizer_attached = local.access_gate_enabled

  provider_acceptance_count = var.environment == "staging" && local.access_gate_enabled ? 1 : 0

  identity_jwt_gate_enforced   = var.identity_jwt_mode == "gate" && local.access_gate_enabled
  identity_jwt_native_enforced = var.identity_jwt_mode == "native"
  identity_jwt_lambda_enforced = var.identity_jwt_mode == "lambda"
  identity_jwt_api_enforced    = local.identity_jwt_native_enforced || local.identity_jwt_lambda_enforced
  api_key_prefix               = "wpk_"

  production_alarms       = var.environment == "production"
  security_alerts_enabled = var.environment == "production"

  runner_image_env_tag = var.environment == "production" ? "production" : "staging"
  runner_image_tag     = coalesce(var.runner_image_tag, local.runner_image_env_tag)
}
