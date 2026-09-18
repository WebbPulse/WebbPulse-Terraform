locals {
  identity_issuer = "https://${local.api_host}/api/auth"

  identity_audience = "${local.prefix}-api"

  identity_registrable_domain = local.host

  identity_oauth_redirect_uris = jsonencode(["${local.identity_issuer}/oauth/callback"])

  identity_webauthn_origins = jsonencode(["https://${local.host}"])
}

variable "identity_rp_name" {
  description = "WebAuthn Relying Party display name shown during a passkey ceremony. A display string only, safe to change at any time."
  type        = string
  default     = "WebbPulse Terraform"
}

variable "passkeys_enabled" {
  description = "Whether the passkey routes are declared. Null derives it from the environment: true in staging, false in production."
  type        = bool
  default     = null
  nullable    = true
}

variable "passkeys_passwordless" {
  description = "Whether a passkey is a way in as well as a credential. Null derives it from the environment: true in staging, false in production."
  type        = bool
  default     = null
  nullable    = true
}

locals {
  passkeys_enabled = var.passkeys_enabled != null ? var.passkeys_enabled : var.environment != "production"

  passkeys_passwordless = var.passkeys_passwordless != null ? var.passkeys_passwordless : var.environment != "production"
}

module "identity" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/identity"
  version = "2.24.0"

  name_prefix        = local.prefix
  issuer             = local.identity_issuer
  audience           = local.identity_audience
  registrable_domain = local.identity_registrable_domain

  identity_role_name = module.lambda_domain["workspaces"].role_id
  identity_role_arn  = module.lambda_domain["workspaces"].role_arn

  enable_mfa_encryption_key = false

  users_stream_enabled = false

  tags = {
    Component = "identity"
  }
  name_tag = true

  point_in_time_recovery = true
  deletion_protection    = var.environment == "production"

  table_policy_actions = local.dynamodb_write_actions
}

output "identity_issuer" {
  description = "Identity issuer, byte identical to the iss claim, the discovery document issuer, and the JWT authorizer issuer"
  value       = local.identity_issuer
}

output "identity_audience" {
  description = "The aud claim the workspaces function stamps on every access token, and the audience the authorizer requires"
  value       = local.identity_audience
}

output "identity_signing_key_arns" {
  description = "Identity signing key ARNs, active signer first. A second entry means a rotation is in progress."
  value       = module.identity.signing_key_arns
}

output "identity_table_names" {
  description = "Logical name to physical name for the identity tables the module creates, for reviewing an apply"
  value       = module.identity.table_names
}
