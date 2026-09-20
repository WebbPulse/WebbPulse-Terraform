module "run_confirmations" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/sqs-queue"
  version = "~> 2.27"

  name = "${local.prefix}-run-confirmations"

  consumer_timeout_seconds   = local.lambda_domain_timeout
  visibility_timeout_seconds = local.lambda_domain_timeout * 6
  max_receive_count          = 5
}
