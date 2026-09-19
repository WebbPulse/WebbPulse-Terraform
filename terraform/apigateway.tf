locals {
  workspaces_routes = merge(
    {
      "GET /api/v1/workspaces"  = { integration = "workspaces" }
      "POST /api/v1/workspaces" = { integration = "workspaces" }
    },
    {
      for method in ["GET", "PATCH", "DELETE"] :
      "${method} /api/v1/workspaces/{workspace_id}" => { integration = "workspaces" }
    },
    {
      "GET /api/v1/workspaces/{workspace_id}/variables"       = { integration = "workspaces" }
      "PUT /api/v1/workspaces/{workspace_id}/variables/{key}" = { integration = "workspaces" }
      "GET /api/v1/workspaces/{workspace_id}/variables/{key}" = { integration = "workspaces" }
    },
    {
      "DELETE /api/v1/workspaces/{workspace_id}/variables/{key}" = { integration = "workspaces" }
    },
    {
      "POST /api/v1/workspaces/{workspace_id}/config-versions" = { integration = "workspaces" }
      "GET /api/v1/workspaces/{workspace_id}/config-versions"  = { integration = "workspaces" }
    },
    {
      "GET /api/v1/workspaces/{workspace_id}/config-versions/{config_version_id}" = { integration = "workspaces" }
    },
  )

  runs_routes = {
    "POST /api/v1/runs"                       = { integration = "runs" }
    "GET /api/v1/runs"                        = { integration = "runs" }
    "GET /api/v1/runs/{run_id}"               = { integration = "runs" }
    "POST /api/v1/runs/{run_id}/confirm"      = { integration = "runs" }
    "POST /api/v1/runs/{run_id}/cancel"       = { integration = "runs" }
    "POST /api/v1/runs/{run_id}/discard"      = { integration = "runs" }
    "GET /api/v1/runs/{run_id}/logs"          = { integration = "runs" }
    "GET /api/v1/runs/{run_id}/bundle"        = { integration = "runs", authorization_type = "NONE" }
    "POST /api/v1/runs/{run_id}/phase-result" = { integration = "runs", authorization_type = "NONE" }
  }

  product_routes = merge(
    { for key, route in local.workspaces_routes : key => merge(route, { require_identity_jwt = true }) },
    {
      for key, route in local.runs_routes :
      key => try(route.authorization_type, null) == "NONE" ? route : merge(route, { require_identity_jwt = true })
    },
  )

  auth_anonymous_routes = {
    "GET /api/auth/.well-known/jwks.json" = {
      integration        = "workspaces"
      authorization_type = "NONE"
    }
    "GET /api/auth/.well-known/openid-configuration" = {
      integration        = "workspaces"
      authorization_type = "NONE"
    }
    "GET /api/auth/oauth/providers" = {
      integration        = "workspaces"
      authorization_type = "NONE"
    }
  }

  auth_routes = merge(
    local.auth_anonymous_routes,
    {
      "GET /api/auth/health"      = { integration = "workspaces" }
      "POST /api/auth/login"      = { integration = "workspaces" }
      "POST /api/auth/register"   = { integration = "workspaces" }
      "POST /api/auth/login/totp" = { integration = "workspaces" }
      "POST /api/auth/refresh"    = { integration = "workspaces" }
      "POST /api/auth/logout"     = { integration = "workspaces" }
    },
    {
      "POST /api/auth/password" = {
        integration          = "workspaces"
        require_identity_jwt = true
      }
      "POST /api/auth/logout-all" = {
        integration          = "workspaces"
        require_identity_jwt = true
      }
      "POST /api/auth/step-up" = {
        integration          = "workspaces"
        require_identity_jwt = true
      }
      "POST /api/auth/totp/enrol" = {
        integration          = "workspaces"
        require_identity_jwt = true
      }
      "POST /api/auth/totp/activate" = {
        integration          = "workspaces"
        require_identity_jwt = true
      }
      "POST /api/auth/totp/disable" = {
        integration          = "workspaces"
        require_identity_jwt = true
      }
      "POST /api/auth/recovery-codes" = {
        integration          = "workspaces"
        require_identity_jwt = true
      }
    },
    local.passkeys_enabled ? {
      "GET /api/auth/passkeys/availability"  = { integration = "workspaces" }
      "POST /api/auth/login/passkey/options" = { integration = "workspaces" }
      "POST /api/auth/login/passkey/verify"  = { integration = "workspaces" }
      "POST /api/auth/passkeys/register/options" = {
        integration          = "workspaces"
        require_identity_jwt = true
      }
      "POST /api/auth/passkeys/register/verify" = {
        integration          = "workspaces"
        require_identity_jwt = true
      }
      "GET /api/auth/passkeys" = {
        integration          = "workspaces"
        require_identity_jwt = true
      }
      "DELETE /api/auth/passkeys/{credential_id}" = {
        integration          = "workspaces"
        require_identity_jwt = true
      }
      "PATCH /api/auth/passkeys/{credential_id}" = {
        integration          = "workspaces"
        require_identity_jwt = true
      }
    } : {},
    local.ephemeral_users_enabled ? {
      "POST /api/auth/e2e/users" = {
        integration          = "workspaces"
        require_identity_jwt = true
      }
      "DELETE /api/auth/e2e/users/{user_id}" = {
        integration          = "workspaces"
        require_identity_jwt = true
      }
    } : {},
  )
}

module "api" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/http-api"
  version = "2.26.1"

  name = "${local.prefix}-api"

  integrations = {
    for name in keys(local.lambda_domains) : name => {
      lambda_function_name = module.lambda_domain[name].function_name
      lambda_invoke_arn    = module.lambda_domain[name].invoke_arn
    }
  }

  default_integration = null

  routes = local.domain_functions_enabled ? merge(
    { "GET /health" = { integration = "workspaces" } },
    local.product_routes,
    local.auth_routes,
  ) : {}

  throttling_burst_limit = 100
  throttling_rate_limit  = 50

  access_log_retention_days = 7

  lambda_permission_statement_id = "AllowAPIGatewayInvoke"

  cors_configuration = {
    allow_origins = split(",", local.cors_origins)
    allow_methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    allow_headers = [
      "Accept",
      "Authorization",
      "Content-Type",
      "Origin",
      "X-Request-ID",
      "X-Retry-Attempt",
    ]
    allow_credentials = true
    max_age           = 86400
  }

  disable_execute_api_endpoint = local.staging_gate_authorizer_attached
  authorizer_id                = local.staging_gate_authorizer_attached ? one(module.staging_access_gate[*].http_api_authorizer_id) : null

  identity_jwt = local.identity_jwt_native_enforced ? {
    issuer   = local.identity_issuer
    audience = local.identity_audience
  } : null

  identity_jwt_depends_on = local.identity_jwt_native_enforced && local.domain_functions_enabled ? [module.lambda_domain["workspaces"]] : []

  domain_name     = local.custom_domains_enabled ? local.api_host : null
  certificate_arn = module.api_certificate.certificate_arn
}
