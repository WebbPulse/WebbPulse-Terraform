module "access_gate" {
  count = local.access_gate_count

  source  = "terraform.webbpulse.com/WebbPulse/platform-modules/aws//modules/staging-access-gate"
  version = "~> 2.33"

  name          = local.access_gate_name
  cookie_domain = local.host
  site_host     = local.host
  session_hours = 168

  allowed_emails = local.access_gate_users

  http_api_id       = module.api.api_id
  http_api_attached = local.access_gate_enabled
  invite_login_url  = "https://${local.host}/"

  invite_email_subject = "WebbPulse Terraform access"
  invite_email_message = "You have been given access to WebbPulse Terraform at https://${local.host}/\n\nUsername: {username}\nTemporary password: {####}\n\nOpen the site, sign in with these, and choose a new password when prompted."
  invite_sms_message   = "WebbPulse Terraform at ${local.host}. Username {username}, temporary password {####}"

  identity_jwt = local.identity_jwt_gate_enforced ? {
    issuer           = local.identity_issuer
    audience         = local.identity_audience
    audiences        = var.device_login_enabled ? [local.identity_audience, local.identity_device_audience] : null
    api_key_prefixes = [local.api_key_prefix]
  } : null

  identity_jwt_route_keys = local.identity_jwt_gate_enforced ? module.api.identity_jwt_route_keys : []
}

moved {
  from = module.staging_access_gate
  to   = module.access_gate
}
