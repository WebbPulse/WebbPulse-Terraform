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
    {
      "GET /api/v1/workspaces/{workspace_id}/state-versions"                    = { integration = "workspaces" }
      "GET /api/v1/workspaces/{workspace_id}/state-versions/{state_version_id}" = { integration = "workspaces" }
    },
    {
      "POST /api/v1/workspaces/{workspace_id}/state-versions/{state_version_id}/download" = { integration = "workspaces" }
    },
    {
      "POST /api/v1/workspaces/{workspace_id}/run-role/check"       = { integration = "workspaces" }
      "GET /api/v1/workspaces/{workspace_id}/run-role/check"        = { integration = "workspaces" }
      "POST /api/v1/workspaces/{workspace_id}/run-role/quick-setup" = { integration = "workspaces" }
    },
    {
      "GET /api/v1/workspaces/{workspace_id}/notification-configurations"  = { integration = "workspaces" }
      "POST /api/v1/workspaces/{workspace_id}/notification-configurations" = { integration = "workspaces" }
    },
    {
      for method in ["GET", "PATCH", "DELETE"] :
      "${method} /api/v1/workspaces/{workspace_id}/notification-configurations/{notification_id}" => { integration = "workspaces" }
    },
    {
      "POST /api/v1/workspaces/{workspace_id}/notification-configurations/{notification_id}/actions/verify" = { integration = "workspaces" }
    },
    {
      "POST /api/v1/api-keys"            = { integration = "workspaces" }
      "GET /api/v1/api-keys"             = { integration = "workspaces" }
      "DELETE /api/v1/api-keys/{key_id}" = { integration = "workspaces" }
    },
  )

  runs_routes = {
    "POST /api/v1/runs"                           = { integration = "runs" }
    "GET /api/v1/runs"                            = { integration = "runs" }
    "GET /api/v1/runs/{run_id}"                   = { integration = "runs" }
    "POST /api/v1/runs/{run_id}/confirm"          = { integration = "runs" }
    "POST /api/v1/runs/{run_id}/cancel"           = { integration = "runs" }
    "POST /api/v1/runs/{run_id}/discard"          = { integration = "runs" }
    "GET /api/v1/runs/{run_id}/logs"              = { integration = "runs" }
    "GET /api/v1/runs/{run_id}/plan"              = { integration = "runs" }
    "GET /api/v1/runs/{run_id}/bundle"            = { integration = "runs", authorization_type = "NONE" }
    "POST /api/v1/runs/{run_id}/artifact-uploads" = { integration = "runs", authorization_type = "NONE" }
    "POST /api/v1/runs/{run_id}/heartbeat"        = { integration = "runs", authorization_type = "NONE" }
    "POST /api/v1/runs/{run_id}/credentials"      = { integration = "runs", authorization_type = "NONE" }
    "POST /api/v1/runs/{run_id}/phase-result"     = { integration = "runs", authorization_type = "NONE" }
    "POST /api/v1/runs/{run_id}/runner-token"     = { integration = "runs", authorization_type = "NONE" }
  }

  github_routes = contains(keys(local.lambda_domains), "github") ? {
    "GET /api/v1/github/app"                                          = { integration = "github" }
    "POST /api/v1/github/app/webhook"                                 = { integration = "github" }
    "POST /api/v1/github/webhooks"                                    = { integration = "github", authorization_type = "NONE" }
    "POST /api/v1/github/app/manifest"                                = { integration = "github" }
    "POST /api/v1/github/app/conversions"                             = { integration = "github" }
    "POST /api/v1/github/install-state"                               = { integration = "github" }
    "POST /api/v1/github/installations"                               = { integration = "github" }
    "GET /api/v1/github/installations"                                = { integration = "github" }
    "POST /api/v1/github/installations/{installation_id}/refresh"     = { integration = "github" }
    "DELETE /api/v1/github/installations/{installation_id}"           = { integration = "github" }
    "GET /api/v1/github/installations/{installation_id}/repositories" = { integration = "github" }
  } : {}

  registry_routes = contains(keys(local.lambda_domains), "registry") ? {
    "GET /v1/modules/{namespace}/{name}/{provider}/versions"                        = { integration = "registry", authorization_type = "NONE" }
    "GET /v1/modules/{namespace}/{name}/{provider}/{version}/download"              = { integration = "registry", authorization_type = "NONE" }
    "GET /api/v1/registry/modules"                                                  = { integration = "registry" }
    "POST /api/v1/registry/modules"                                                 = { integration = "registry" }
    "GET /api/v1/registry/modules/{namespace}/{name}/{provider}"                    = { integration = "registry" }
    "GET /api/v1/registry/modules/{namespace}/{name}/{provider}/versions/{version}" = { integration = "registry" }
    "POST /api/v1/registry/modules/{namespace}/{name}/{provider}/resync"            = { integration = "registry" }
    "DELETE /api/v1/registry/modules/{namespace}/{name}/{provider}"                 = { integration = "registry" }
    "GET /v1/providers/{namespace}/{type}/versions"                                 = { integration = "registry", authorization_type = "NONE" }
    "GET /v1/providers/{namespace}/{type}/{version}/download/{os}/{arch}"           = { integration = "registry", authorization_type = "NONE" }
    "GET /api/v1/registry/providers"                                                = { integration = "registry" }
    "POST /api/v1/registry/providers"                                               = { integration = "registry" }
    "GET /api/v1/registry/providers/{namespace}/{type}"                             = { integration = "registry" }
    "POST /api/v1/registry/providers/{namespace}/{type}/resync"                     = { integration = "registry" }
    "DELETE /api/v1/registry/providers/{namespace}/{type}"                          = { integration = "registry" }
  } : {}

  terraform_login_routes = {
    "POST /api/v1/oauth/authorizations" = { integration = "workspaces", require_identity_jwt = true }
    "POST /v1/oauth/token"              = { integration = "workspaces", authorization_type = "NONE" }
  }

  tfe_routes = {
    "GET /api/v2/ping"                                                     = { integration = "workspaces", authorization_type = "NONE" }
    "GET /api/v2/organizations/{organization}/entitlement-set"             = { integration = "workspaces", authorization_type = "NONE" }
    "GET /api/v2/organizations/{organization}/workspaces"                  = { integration = "workspaces", authorization_type = "NONE" }
    "POST /api/v2/organizations/{organization}/workspaces"                 = { integration = "workspaces", authorization_type = "NONE" }
    "GET /api/v2/organizations/{organization}/workspaces/{workspace_name}" = { integration = "workspaces", authorization_type = "NONE" }
    "GET /api/v2/workspaces/{workspace_id}"                                = { integration = "workspaces", authorization_type = "NONE" }
    "GET /api/v2/workspaces/{workspace_id}/all-vars"                       = { integration = "workspaces", authorization_type = "NONE" }
    "POST /api/v2/workspaces/{workspace_id}/configuration-versions"        = { integration = "workspaces", authorization_type = "NONE" }
    "GET /api/v2/configuration-versions/{config_version_id}"               = { integration = "workspaces", authorization_type = "NONE" }
    "POST /api/v2/workspaces/{workspace_id}/actions/lock"                  = { integration = "workspaces", authorization_type = "NONE" }
    "POST /api/v2/workspaces/{workspace_id}/actions/unlock"                = { integration = "workspaces", authorization_type = "NONE" }
    "POST /api/v2/workspaces/{workspace_id}/actions/force-unlock"          = { integration = "workspaces", authorization_type = "NONE" }
    "GET /api/v2/workspaces/{workspace_id}/current-state-version"          = { integration = "workspaces", authorization_type = "NONE" }
    "GET /api/v2/workspaces/{workspace_id}/current-state-version-outputs"  = { integration = "workspaces", authorization_type = "NONE" }
    "POST /api/v2/workspaces/{workspace_id}/state-versions"                = { integration = "workspaces", authorization_type = "NONE" }
    "GET /api/v2/state-versions/{state_version_id}"                        = { integration = "workspaces", authorization_type = "NONE" }
    "GET /api/v2/state-versions/{state_version_id}/download"               = { integration = "workspaces", authorization_type = "NONE" }
    "GET /api/v2/state-version-outputs/{output_id}"                        = { integration = "workspaces", authorization_type = "NONE" }
  }

  tfe_runs_routes = contains(keys(local.lambda_domains), "runs") ? {
    "POST /api/v2/runs"                                   = { integration = "runs", authorization_type = "NONE" }
    "GET /api/v2/runs/{run_id}"                           = { integration = "runs", authorization_type = "NONE" }
    "GET /api/v2/runs/{run_id}/run-events"                = { integration = "runs", authorization_type = "NONE" }
    "POST /api/v2/runs/{run_id}/actions/apply"            = { integration = "runs", authorization_type = "NONE" }
    "POST /api/v2/runs/{run_id}/actions/discard"          = { integration = "runs", authorization_type = "NONE" }
    "POST /api/v2/runs/{run_id}/actions/cancel"           = { integration = "runs", authorization_type = "NONE" }
    "GET /api/v2/workspaces/{workspace_id}/runs"          = { integration = "runs", authorization_type = "NONE" }
    "GET /api/v2/plans/{plan_id}"                         = { integration = "runs", authorization_type = "NONE" }
    "GET /api/v2/plans/{plan_id}/logs/{token}"            = { integration = "runs", authorization_type = "NONE" }
    "GET /api/v2/applies/{apply_id}"                      = { integration = "runs", authorization_type = "NONE" }
    "GET /api/v2/applies/{apply_id}/logs/{token}"         = { integration = "runs", authorization_type = "NONE" }
    "GET /api/v2/organizations/{organization}/runs/queue" = { integration = "runs", authorization_type = "NONE" }
    "GET /api/v2/organizations/{organization}/capacity"   = { integration = "runs", authorization_type = "NONE" }
  } : {}

  product_routes = merge(
    { for key, route in local.workspaces_routes : key => merge(route, { require_identity_jwt = true }) },
    {
      for key, route in local.runs_routes :
      key => try(route.authorization_type, null) == "NONE" ? route : merge(route, { require_identity_jwt = true })
    },
    {
      for key, route in local.github_routes :
      key => try(route.authorization_type, null) == "NONE" ? route : merge(route, { require_identity_jwt = true })
    },
    {
      for key, route in local.registry_routes :
      key => try(route.authorization_type, null) == "NONE" ? route : merge(route, { require_identity_jwt = true })
    },
    local.terraform_login_routes,
    local.tfe_routes,
    local.tfe_runs_routes,
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
      "POST /api/auth/step-up/passkey/options" = {
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
      "GET /api/auth/passkeys/availability" = { integration = "workspaces" }
    },
    local.passkeys_enabled ? {
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
    var.device_login_enabled ? {
      "POST /api/auth/device/code"                = { integration = "workspaces" }
      "POST /api/auth/device/token"               = { integration = "workspaces" }
      "POST /api/auth/device/revoke"              = { integration = "workspaces" }
      "GET /api/auth/device"                      = { integration = "workspaces" }
      "POST /api/auth/device/approve"             = { integration = "workspaces" }
      "GET /api/auth/device/grants"               = { integration = "workspaces" }
      "DELETE /api/auth/device/grants/{grant_id}" = { integration = "workspaces" }
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
      "POST /api/auth/e2e/users/sweep" = {
        integration          = "workspaces"
        require_identity_jwt = true
      }
    } : {},
  )
}

module "api" {
  source  = "terraform.webbpulse.com/WebbPulse/platform-modules/aws//modules/http-api"
  version = "~> 2.33"

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

  disable_execute_api_endpoint = local.custom_domains_enabled
  authorizer_id                = local.access_gate_authorizer_attached ? one(module.access_gate[*].http_api_authorizer_id) : null

  identity_jwt = local.identity_jwt_api_enforced && local.domain_functions_enabled ? {
    issuer           = local.identity_issuer
    audience         = local.identity_audience
    mode             = var.identity_jwt_mode
    api_key_prefixes = local.identity_jwt_lambda_enforced ? [local.api_key_prefix] : []
  } : null

  identity_jwt_depends_on = local.identity_jwt_api_enforced && local.domain_functions_enabled ? [module.lambda_domain["workspaces"]] : []

  domain_name     = local.custom_domains_enabled ? local.api_host : null
  certificate_arn = module.api_certificate.certificate_arn
}
