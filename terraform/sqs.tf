module "run_confirmations" {
  source  = "terraform.webbpulse.com/WebbPulse/platform-modules/aws//modules/sqs-queue"
  version = "~> 2.33"

  name = "${local.prefix}-run-confirmations"

  consumer_timeout_seconds   = local.lambda_domain_timeout
  visibility_timeout_seconds = local.lambda_domain_timeout * 6
  max_receive_count          = 5
}
