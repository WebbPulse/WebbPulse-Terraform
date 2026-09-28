module "access_gate" {
  count = local.access_gate_count

  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/staging-access-gate"
  version = "~> 2.32"

  name          = local.access_gate_name
  cookie_domain = local.host
  site_host     = local.host

  allowed_emails = local.access_gate_users

  http_api_id       = module.api.api_id
  http_api_attached = local.access_gate_enabled
  invite_login_url  = "https://${local.host}/"

  identity_jwt = local.identity_jwt_gate_enforced ? {
    issuer           = local.identity_issuer
    audience         = local.identity_audience
    api_key_prefixes = [local.api_key_prefix]
  } : null

  identity_jwt_route_keys = local.identity_jwt_gate_enforced ? module.api.identity_jwt_route_keys : []
}

moved {
  from = module.staging_access_gate
  to   = module.access_gate
}
