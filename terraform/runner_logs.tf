resource "aws_cloudwatch_log_group" "runner" {
  name              = "/${local.project}/${local.env_slug}/runner"
  retention_in_days = 30
}
