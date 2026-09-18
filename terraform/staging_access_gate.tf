variable "staging_gate_attach_api_authorizer" {
  description = <<-EOT
    Attach the gate's origin-verify authorizer to the HTTP API. It is false on a greenfield
    environment and true on every later apply. The module decides whether to create the
    authorizer with count on http_api_id, and the api id is unknown until the API exists, so a
    first plan with this on fails with an invalid count argument rather than a cycle. Apply once
    with it off, then turn it on; the API's routes carry authorization_type NONE until it is on.
  EOT
  type        = bool
  default     = false
}

module "staging_access_gate" {
  count = local.staging_gate_count

  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/staging-access-gate"
  version = "2.24.0"

  name          = "${local.project}-stg"
  cookie_domain = local.host
  site_host     = local.host

  allowed_emails = var.staging_access_users

  http_api_id      = var.staging_gate_attach_api_authorizer ? module.api.api_id : null
  invite_login_url = "https://${local.host}/"

  identity_jwt = local.identity_jwt_gate_enforced ? {
    issuer   = local.identity_issuer
    audience = local.identity_audience
  } : null

  identity_jwt_route_keys = local.identity_jwt_gate_enforced ? module.api.identity_jwt_route_keys : []
}
