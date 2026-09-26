module "vcs_ingest" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/sqs-queue"
  version = "~> 2.27"

  name = "${local.prefix}-vcs-ingest"

  consumer_timeout_seconds   = local.lambda_domain_timeout
  visibility_timeout_seconds = local.lambda_domain_timeout * 6
  max_receive_count          = 5
}

resource "aws_cloudwatch_event_rule" "vcs_ingest_object_created" {
  name        = "${local.prefix}-vcs-ingest-object-created"
  description = "Config tarballs a GitHub Actions workflow uploaded under ingest/ through a presigned VCS upload"

  event_pattern = jsonencode({
    source      = ["aws.s3"]
    detail-type = ["Object Created"]
    detail = {
      bucket = { name = [module.artifacts.bucket] }
      object = { key = [{ prefix = "ingest/" }] }
    }
  })

  tags = local.common_tags
}

resource "aws_cloudwatch_event_target" "vcs_ingest_object_created" {
  rule      = aws_cloudwatch_event_rule.vcs_ingest_object_created.name
  target_id = "vcs-ingest-queue"
  arn       = module.vcs_ingest.queue_arn

  input_transformer {
    input_paths = {
      bucket = "$.detail.bucket.name"
      key    = "$.detail.object.key"
      size   = "$.detail.object.size"
    }

    input_template = <<-EOT
      {"kind":"config_ingested","bucket":"<bucket>","key":"<key>","size":<size>}
    EOT
  }
}

data "aws_iam_policy_document" "vcs_ingest_queue" {
  statement {
    sid       = "LetEventBridgeDeliverIngestUploads"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [module.vcs_ingest.queue_arn]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.vcs_ingest_object_created.arn]
    }
  }
}

resource "aws_sqs_queue_policy" "vcs_ingest" {
  queue_url = module.vcs_ingest.queue_url
  policy    = data.aws_iam_policy_document.vcs_ingest_queue.json
}
