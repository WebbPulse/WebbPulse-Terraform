module "site_certificate" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/acm-certificate"
  version = "2.24.0"

  providers = {
    aws         = aws.us_east_1
    aws.records = aws.dns
  }

  enabled     = local.custom_domains_enabled
  domain_name = local.host
  zone_id     = local.records_zone_id
}

module "api_certificate" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/acm-certificate"
  version = "2.24.0"

  providers = {
    aws         = aws
    aws.records = aws.dns
  }

  enabled     = local.custom_domains_enabled
  domain_name = local.api_host
  zone_id     = local.records_zone_id
}
