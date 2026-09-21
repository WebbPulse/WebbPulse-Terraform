provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.common_tags
  }
}

provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = local.common_tags
  }
}

provider "aws" {
  alias  = "dns"
  region = var.aws_region

  dynamic "assume_role" {
    for_each = local.workload_dns_role_arn == "" ? [] : [local.workload_dns_role_arn]

    content {
      role_arn = assume_role.value
    }
  }

  default_tags {
    tags = local.common_tags
  }
}

provider "aws" {
  alias  = "parent_dns"
  region = var.aws_region

  dynamic "assume_role" {
    for_each = var.route53_write_role_arn == "" ? [] : [var.route53_write_role_arn]

    content {
      role_arn = assume_role.value
    }
  }

  default_tags {
    tags = local.common_tags
  }
}
