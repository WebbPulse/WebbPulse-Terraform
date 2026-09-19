module "vpc" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/vpc-public"
  version = "2.26.1"

  name         = "${local.prefix}-runner"
  cidr_block   = var.vpc_cidr_block
  subnet_count = 2

  enable_s3_gateway_endpoint       = true
  enable_dynamodb_gateway_endpoint = true
}
