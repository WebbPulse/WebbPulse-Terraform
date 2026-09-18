module "staging_access_gate" {
  count = local.staging_gate_count

  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/staging-access-gate"
  version = "2.25.1"

  name          = "${local.project}-stg"
  cookie_domain = local.host
  site_host     = local.host

  allowed_emails = var.staging_access_users

  http_api_id       = module.api.api_id
  http_api_attached = var.staging_access_gate
  invite_login_url  = "https://${local.host}/"

  identity_jwt = local.identity_jwt_gate_enforced ? {
    issuer   = local.identity_issuer
    audience = local.identity_audience
  } : null

  identity_jwt_route_keys = local.identity_jwt_gate_enforced ? module.api.identity_jwt_route_keys : []
}
