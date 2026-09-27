locals {
  registry_repositories = merge(
    { "WebbPulse/terraform-aws-platform-modules" = "" },
    var.environment == "staging" ? { "WebbPulse/webbpulse-terraform-staging-e2e" = "registry-proof/null" } : {},
  )
}

module "registry_ingest" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/sqs-queue"
  version = "~> 2.27"

  name = "${local.prefix}-registry-ingest"

  consumer_timeout_seconds   = local.lambda_domain_timeout
  visibility_timeout_seconds = local.lambda_domain_timeout * 6
  max_receive_count          = 5
}

resource "aws_cloudwatch_event_rule" "registry_ingest_object_created" {
  name        = "${local.prefix}-registry-ingest-object-created"
  description = "Module tarballs a tag push workflow uploaded under registry/incoming/ through a presigned registry upload"

  event_pattern = jsonencode({
    source      = ["aws.s3"]
    detail-type = ["Object Created"]
    detail = {
      bucket = { name = [module.artifacts.bucket] }
      object = { key = [{ prefix = "registry/incoming/" }] }
    }
  })

  tags = local.common_tags
}

resource "aws_cloudwatch_event_target" "registry_ingest_object_created" {
  rule      = aws_cloudwatch_event_rule.registry_ingest_object_created.name
  target_id = "registry-ingest-queue"
  arn       = module.registry_ingest.queue_arn

  input_transformer {
    input_paths = {
      bucket = "$.detail.bucket.name"
      key    = "$.detail.object.key"
      size   = "$.detail.object.size"
    }

    input_template = <<-EOT
      {"kind":"module_ingested","bucket":"<bucket>","key":"<key>","size":<size>}
    EOT
  }
}

data "aws_iam_policy_document" "registry_ingest_queue" {
  statement {
    sid       = "LetEventBridgeDeliverModuleUploads"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [module.registry_ingest.queue_arn]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.registry_ingest_object_created.arn]
    }
  }
}

resource "aws_sqs_queue_policy" "registry_ingest" {
  queue_url = module.registry_ingest.queue_url
  policy    = data.aws_iam_policy_document.registry_ingest_queue.json
}
