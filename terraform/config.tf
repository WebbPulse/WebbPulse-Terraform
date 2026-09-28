module "config" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/operator-config"
  version = "~> 2.33"

  name_prefix = local.prefix
}
