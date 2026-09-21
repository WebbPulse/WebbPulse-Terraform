resource "random_password" "secret_key" {
  length           = 64
  special          = true
  override_special = null
  min_special      = 0
  min_numeric      = 0
  min_upper        = 0
  min_lower        = 0
}

module "app_secrets" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/app-secrets"
  version = "~> 2.27"

  name_prefix = local.prefix

  json_generate_carry_enabled = local.domain_functions_enabled

  secrets = {
    "app" = {
      description = "JSON map of runtime secrets read by the control plane Lambdas at cold start"
      version     = 2
      json = {
        SECRET_KEY = random_password.secret_key.result
      }
      json_generate = {
        variables_master_key = {
          format = "bytes32-base64"
          keep   = true
        }
        mfa_master_key = {
          format = "bytes32-base64"
          keep   = true
        }
      }
    }
  }
}
