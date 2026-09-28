module "github_webhooks" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/sqs-queue"
  version = "~> 2.33"

  name = "${local.prefix}-github-webhooks"

  consumer_timeout_seconds   = local.lambda_domain_timeout
  visibility_timeout_seconds = local.lambda_domain_timeout * 6
  max_receive_count          = 5
}
