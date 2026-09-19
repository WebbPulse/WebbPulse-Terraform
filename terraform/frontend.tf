module "frontend" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/spa-frontend"
  version = "2.26.0"

  name = "${local.prefix}-frontend"

  aliases             = local.custom_domains_enabled ? [local.host] : []
  acm_certificate_arn = module.site_certificate.certificate_arn

  cache_mode            = "forwarded_values"
  error_caching_min_ttl = 10

  index_cache_mode     = "policies"
  index_cache_policies = { cache_policy_id = "658327ea-f89d-4fab-a63d-7e88639e58f6" }

  access_gate = local.staging_gate_enabled ? {
    key_group_id                                           = module.staging_access_gate[0].key_group_id
    viewer_request_function_arn                            = module.staging_access_gate[0].viewer_request_function_arn
    login_origin_domain_name                               = module.staging_access_gate[0].login_origin_domain_name
    login_origin_access_control_id                         = module.staging_access_gate[0].login_origin_access_control_id
    auth_path_pattern                                      = module.staging_access_gate[0].auth_path_pattern
    cache_policy_id_caching_disabled                       = module.staging_access_gate[0].cache_policy_id_caching_disabled
    origin_request_policy_id_all_viewer_except_host_header = module.staging_access_gate[0].origin_request_policy_id_all_viewer_except_host_header
  } : null

  create_dns_records = false
}
