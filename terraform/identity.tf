locals {
  identity_issuer = "https://${local.api_host}/api/auth"

  identity_audience = "${local.prefix}-api"

  identity_registrable_domain = local.host

  identity_oauth_redirect_uris = jsonencode(["${local.identity_issuer}/oauth/callback"])

  identity_webauthn_origins = jsonencode(["https://${local.host}"])

  identity_device_audience = "${local.identity_issuer}/device"

  identity_device_scopes = [
    "workspaces:read",
    "workspaces:write",
    "variables:read",
    "variables:write",
    "configs:read",
    "configs:write",
    "runs:read",
    "runs:write",
    "runs:apply",
    "state:download",
    "registry:read",
    "registry:write",
    "admin",
  ]

  identity_device_explicit_scopes = ["runs:apply", "state:download", "admin"]

  identity_device_environment = var.device_login_enabled ? {
    IDENTITY_DEVICE_GRANT_ENABLED    = "true"
    IDENTITY_DEVICE_CLIENTS          = jsonencode({ "wp-tf" = "wp-tf CLI" })
    IDENTITY_DEVICE_SCOPES_SUPPORTED = jsonencode(local.identity_device_scopes)
    IDENTITY_DEVICE_EXPLICIT_SCOPES  = jsonencode(local.identity_device_explicit_scopes)
    IDENTITY_DEVICE_AUDIENCE         = local.identity_device_audience
    IDENTITY_DEVICE_LOGIN_URL        = "https://${local.host}/sign-in"
  } : {}
}

variable "device_login_enabled" {
  description = "Whether `wp-tf login` can sign a person in through the OAuth device grant. On in every environment."
  type        = bool
  default     = true
}

variable "identity_rp_name" {
  description = "WebAuthn Relying Party display name shown during a passkey ceremony. A display string only, safe to change at any time."
  type        = string
  default     = "WebbPulse Terraform"
}

variable "passkeys_enabled" {
  description = "Whether the passkey routes are declared. On in every environment."
  type        = bool
  default     = true
}

variable "ephemeral_users_enabled" {
  description = "Whether the e2e ephemeral user routes are declared. Null derives it from the environment: true outside production, false in production."
  type        = bool
  default     = null
  nullable    = true
}

variable "passkeys_passwordless" {
  description = "Whether a passkey is a way in as well as a credential. On in every environment."
  type        = bool
  default     = true
}

locals {
  ephemeral_users_enabled = var.ephemeral_users_enabled != null ? var.ephemeral_users_enabled : var.environment != "production"

  passkeys_enabled = var.passkeys_enabled

  passkeys_passwordless = var.passkeys_enabled && var.passkeys_passwordless
}

module "identity" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/identity"
  version = "~> 2.33"

  name_prefix        = local.prefix
  issuer             = local.identity_issuer
  audience           = local.identity_audience
  registrable_domain = local.identity_registrable_domain

  identity_role_name = local.domain_functions_enabled ? module.lambda_domain["workspaces"].role_id : null
  identity_role_arn  = local.domain_functions_enabled ? module.lambda_domain["workspaces"].role_arn : null

  attach_role_policies = local.domain_functions_enabled

  enable_mfa_encryption_key = false

  api_keys_table_enabled = true
  api_keys_table = {
    attributes = [
      { name = "key_hash", type = "S" },
      { name = "user_id", type = "S" },
      { name = "tenant_id", type = "S" },
      { name = "created_at", type = "S" },
    ]
    hash_key = "key_hash"
    global_secondary_indexes = [
      {
        name      = "user_id-created_at-index"
        hash_key  = "user_id"
        range_key = "created_at"
      },
      {
        name      = "tenant_id-created_at-index"
        hash_key  = "tenant_id"
        range_key = "created_at"
      },
    ]
    ttl_attribute = "purge_at"
  }

  oauth_server_enabled = true
  oauth_server_tables = {
    "authorization-codes" = {
      attributes             = [{ name = "code_hash", type = "S" }]
      hash_key               = "code_hash"
      ttl_attribute          = "expires_at"
      point_in_time_recovery = false
    }
  }

  users_stream_enabled   = local.domain_functions_enabled
  users_table_stream_arn = local.domain_functions_enabled ? module.dynamodb.stream_arns["users"] : null
  identity_function_name = local.domain_functions_enabled ? module.lambda_domain["workspaces"].function_name : null
  users_key_attribute    = "id"

  tags = {
    Component = "identity"
  }
  name_tag = true

  point_in_time_recovery = true
  deletion_protection    = var.environment == "production"

  table_policy_actions = local.dynamodb_write_actions

  additional_table_grants = local.identity_additional_table_grants

  device_grant_enabled = var.device_login_enabled
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
    } : {}, contains(keys(local.lambda_domains), "registry") ? {
    registry = {
      role_name = module.lambda_domain["registry"].role_id
      tables    = ["api-keys"]
      actions   = concat(local.dynamodb_read_actions, ["dynamodb:UpdateItem"])
    }
    } : {}, var.device_login_enabled ? {
    for name in setsubtract(keys(local.lambda_domains), ["workspaces"]) : "${name}-device-grants" => {
      role_name = module.lambda_domain[name].role_id
      tables    = ["device-grants"]
      actions   = local.dynamodb_read_actions
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
