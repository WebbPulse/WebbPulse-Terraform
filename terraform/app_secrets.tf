variable "variables_master_key_keep" {
  description = "Whether the app secret's variables_master_key is read back and written through unchanged. It must stay false until the secret has its first version, because the read fails on a secret with no version, and must be turned on before any later bump of the secret's version, because rotating that key makes every stored sensitive workspace variable unreadable."
  type        = bool
  default     = false
}

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
  version = "2.24.0"

  name_prefix = local.prefix

  secrets = {
    "app" = {
      description = "JSON map of runtime secrets read by the control plane Lambdas at cold start"
      version     = 1
      json = {
        SECRET_KEY = random_password.secret_key.result
      }
      json_generate = {
        variables_master_key = {
          format = "bytes32-base64"
          keep   = var.variables_master_key_keep
        }
      }
    }
  }
}
