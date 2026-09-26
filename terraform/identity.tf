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

variable "ephemeral_users_enabled" {
  description = "Whether the e2e ephemeral user routes are declared. Null derives it from the environment: true outside production, false in production."
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
  ephemeral_users_enabled = var.ephemeral_users_enabled != null ? var.ephemeral_users_enabled : var.environment != "production"

  passkeys_enabled = var.passkeys_enabled != null ? var.passkeys_enabled : var.environment != "production"

  passkeys_passwordless = var.passkeys_passwordless != null ? var.passkeys_passwordless : var.environment != "production"
}

module "identity" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/identity"
  version = "2.28.0"

  name_prefix        = local.prefix
  issuer             = local.identity_issuer
  audience           = local.identity_audience
  registrable_domain = local.identity_registrable_domain

  identity_role_name = local.domain_functions_enabled ? module.lambda_domain["workspaces"].role_id : null
  identity_role_arn  = local.domain_functions_enabled ? module.lambda_domain["workspaces"].role_arn : null

  attach_role_policies = local.domain_functions_enabled

  enable_mfa_encryption_key = false

  api_keys_table_enabled = true

  users_stream_enabled = false

  tags = {
    Component = "identity"
  }
  name_tag = true

  point_in_time_recovery = true
  deletion_protection    = var.environment == "production"

  table_policy_actions = local.dynamodb_write_actions

  additional_table_grants = local.identity_additional_table_grants
}

locals {
  identity_additional_table_grants = local.domain_functions_enabled ? merge({
    runs = {
      role_name = module.lambda_domain["runs"].role_id
      tables    = ["api-keys"]
      actions   = local.dynamodb_write_actions
    }
    }, contains(keys(local.lambda_domains), "github") ? {
    github = {
      role_name = module.lambda_domain["github"].role_id
      tables    = ["api-keys"]
      actions   = concat(local.dynamodb_read_actions, ["dynamodb:UpdateItem"])
    }
  } : {}) : {}
}

output "identity_api_keys_table_name" {
  description = "Physical name of the identity api-keys table, which holds agent keys and run tokens alike"
  value       = module.identity.api_keys_table_name
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
