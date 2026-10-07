locals {
  registry_timeout = 180
}

module "registry_ingest" {
  source  = "terraform.webbpulse.com/WebbPulse/platform-modules/aws//modules/sqs-queue"
  version = "~> 2.33"

  name = "${local.prefix}-registry-ingest"

  consumer_timeout_seconds   = local.registry_timeout
  visibility_timeout_seconds = local.registry_timeout * 6
  max_receive_count          = 5
}
