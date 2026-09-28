resource "aws_sns_topic" "aws_connect" {
  name = "${local.prefix}-aws-connect"
}

data "aws_iam_policy_document" "aws_connect_topic" {
  statement {
    sid       = "CloudFormationServicePublishes"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.aws_connect.arn]

    principals {
      type        = "Service"
      identifiers = ["cloudformation.amazonaws.com"]
    }
  }

  statement {
    sid       = "AnyAccountPublishesThroughCloudFormation"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.aws_connect.arn]

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    condition {
      test     = "ForAnyValue:StringEquals"
      variable = "aws:CalledVia"
      values   = ["cloudformation.amazonaws.com"]
    }
  }
}

resource "aws_sns_topic_policy" "aws_connect" {
  arn    = aws_sns_topic.aws_connect.arn
  policy = data.aws_iam_policy_document.aws_connect_topic.json
}

module "aws_connect" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/sqs-queue"
  version = "~> 2.33"

  name = "${local.prefix}-aws-connect"

  consumer_timeout_seconds   = local.lambda_domain_timeout
  visibility_timeout_seconds = local.lambda_domain_timeout * 6
  max_receive_count          = 3
  message_retention_seconds  = 3600
}

data "aws_iam_policy_document" "aws_connect_queue" {
  statement {
    sid       = "TheConnectTopicDelivers"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [module.aws_connect.queue_arn]

    principals {
      type        = "Service"
      identifiers = ["sns.amazonaws.com"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_sns_topic.aws_connect.arn]
    }
  }
}

resource "aws_sqs_queue_policy" "aws_connect" {
  queue_url = module.aws_connect.queue_url
  policy    = data.aws_iam_policy_document.aws_connect_queue.json
}

resource "aws_sns_topic_subscription" "aws_connect" {
  topic_arn            = aws_sns_topic.aws_connect.arn
  protocol             = "sqs"
  endpoint             = module.aws_connect.queue_arn
  raw_message_delivery = true

  depends_on = [aws_sqs_queue_policy.aws_connect]
}
