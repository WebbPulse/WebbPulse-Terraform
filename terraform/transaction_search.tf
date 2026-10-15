module "transaction_search" {
  source  = "terraform.webbpulse.com/WebbPulse/platform-modules/aws//modules/transaction-search"
  version = "~> 2.41"

  name_prefix = local.prefix

  adopt_spans_log_group             = var.adopt_spans_log_group
  spans_log_group_retention_in_days = 7

  adopt_application_signals_log_group = var.adopt_application_signals_log_group
  span_sample_ratio                   = 0.1
}

import {
  for_each = var.adopt_spans_log_group ? toset(["aws/spans"]) : toset([])

  to = module.transaction_search.aws_cloudwatch_log_group.spans[each.key]
  id = each.value
}

import {
  for_each = var.adopt_application_signals_log_group ? toset(["/aws/application-signals/data"]) : toset([])

  to = module.transaction_search.aws_cloudwatch_log_group.application_signals[each.key]
  id = each.value
}

moved {
  from = aws_cloudwatch_log_resource_policy.transaction_search_spans
  to   = module.transaction_search.aws_cloudwatch_log_resource_policy.spans
}

moved {
  from = aws_xray_trace_segment_destination.main
  to   = module.transaction_search.aws_xray_trace_segment_destination.this
}

moved {
  from = aws_xray_indexing_rule.default
  to   = module.transaction_search.aws_xray_indexing_rule.default[0]
}

moved {
  from = aws_cloudwatch_log_group.spans["aws/spans"]
  to   = module.transaction_search.aws_cloudwatch_log_group.spans["aws/spans"]
}
