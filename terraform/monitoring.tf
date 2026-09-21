module "alarms" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/api-alarms"
  version = "~> 2.27"

  name_prefix         = local.prefix
  notification_emails = local.production_alarms ? ["tyler@webbpulse.com", "tylert2610@gmail.com"] : []

  http_api_id = module.api.api_id

  alarms = {
    api_5xx                  = local.production_alarms
    lambda_account_errors    = local.production_alarms
    lambda_account_throttles = local.production_alarms
  }
}
