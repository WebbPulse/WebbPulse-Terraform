module "staging_dns" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/staging-dns"
  version = "2.26.1"

  providers = {
    aws        = aws
    aws.parent = aws.parent_dns
  }

  enabled        = var.environment != "production" && local.custom_domains_enabled
  zone_name      = local.host
  parent_zone_id = var.route53_zone_id
}

resource "aws_route53_record" "site" {
  count    = local.custom_domain_count
  provider = aws.dns

  zone_id = local.records_zone_id
  name    = local.host
  type    = "A"

  alias {
    name                   = module.frontend.distribution_domain_name
    zone_id                = module.frontend.distribution_hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "api" {
  count    = local.custom_domain_count
  provider = aws.dns

  zone_id = local.records_zone_id
  name    = local.api_host
  type    = "A"

  alias {
    name                   = module.api.custom_domain_target_domain_name
    zone_id                = module.api.custom_domain_hosted_zone_id
    evaluate_target_health = false
  }
}
