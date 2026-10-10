removed {
  from = random_password.secret_key

  lifecycle {
    destroy = false
  }
}

module "app_secrets" {
  source  = "terraform.webbpulse.com/WebbPulse/platform-modules/aws//modules/app-secrets"
  version = "~> 2.33"

  name_prefix = local.prefix

  json_generate_carry_enabled = local.domain_functions_enabled

  secrets = {
    "app" = {
      description = "JSON map of runtime secrets read by the control plane Lambdas at cold start"
      version     = 3

      json_preserve_unmanaged = true
      json_generate = {
        SECRET_KEY = {
          format  = "password"
          length  = 64
          special = true
          keep    = true
        }
        variables_master_key = {
          format = "bytes32-base64"
          keep   = true
        }
        mfa_master_key = {
          format = "bytes32-base64"
          keep   = true
        }
        GITHUB_WEBHOOK_SECRET = {
          format  = "password"
          length  = 48
          special = false
          keep    = true
        }
      }
    }
  }
}
