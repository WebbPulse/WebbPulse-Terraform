output "aws_account_id" {
  description = "AWS account ID Terraform is deploying into"
  value       = data.aws_caller_identity.current.account_id
}

output "aws_region" {
  description = "AWS region being deployed to"
  value       = data.aws_region.current.region
}

output "frontend_url" {
  description = "Public control plane URL"
  value       = local.frontend_url
}

output "frontend_api_base_url" {
  description = "Base URL the frontend build must call, set as the API_BASE_URL GitHub environment variable"
  value       = local.api_url
}

output "backend_url" {
  description = "Public API base URL (custom domain when enabled, otherwise the HTTP API endpoint)"
  value       = local.api_url
}

output "cloudfront_distribution_id" {
  description = "CloudFront distribution ID, set as the CLOUDFRONT_DISTRIBUTION_ID GitHub environment variable"
  value       = module.frontend.distribution_id
}

output "frontend_bucket" {
  description = "S3 bucket name for frontend asset uploads, set as the FRONTEND_S3_BUCKET GitHub environment variable"
  value       = module.frontend.bucket_name
}

output "api_id" {
  description = "Id of the HTTP API, set as the API_ID GitHub environment variable so the e2e suite can read the deployed route keys"
  value       = module.api.api_id
}

output "api_access_log_group_name" {
  description = "Name of the HTTP API access log group, set as the API_ACCESS_LOG_GROUP GitHub environment variable"
  value       = module.api.access_log_group_name
}

output "github_actions_deploy_role_arn" {
  description = "ARN of the deploy role both deploy workflows assume, set as the AWS_DEPLOY_ROLE_ARN GitHub environment variable"
  value       = module.github_actions_role.role_arn
}

output "github_actions_ci_role_arn" {
  description = "ARN of the read-only CodeArtifact role pull request CI assumes, set as the CI_AWS_ROLE_ARN repository variable"
  value       = module.github_actions_ci_role.role_arn
}

output "domain_lambda_function_names" {
  description = "Lambda function name keyed by domain, used to build the function-image map for UpdateFunctionCode"
  value       = { for name, fn in module.lambda_domain : name => fn.function_name }
}

output "dynamodb_table_names" {
  description = "DynamoDB table names keyed by the contract's logical name"
  value       = module.dynamodb.table_names
}

output "state_bucket" {
  description = "Bucket holding workspace state at workspaces/<workspace_id>/terraform.tfstate"
  value       = module.state.bucket
}

output "state_bucket_kms_key_arn" {
  description = "KMS key encrypting workspace state, which a runner's S3 backend configuration names"
  value       = module.state.kms_key_arn
}

output "artifacts_bucket" {
  description = "Bucket holding config tarballs, plan artifacts and phase logs"
  value       = module.artifacts.bucket
}

output "artifacts_bucket_kms_key_arn" {
  description = "KMS key encrypting the artifacts bucket"
  value       = module.artifacts.kms_key_arn
}

output "runner_log_group_name" {
  description = "Log group the runner writes phase output to, streamed back by GET /runs/{id}/logs"
  value       = aws_cloudwatch_log_group.runner.name
}

output "run_state_machine_arn" {
  description = "ARN of the run state machine, one execution per run"
  value       = module.run_state_machine.arn
}

output "runner_cluster_arn" {
  description = "ECS cluster the plan and apply tasks launch into"
  value       = module.runner.cluster_arn
}

output "runner_task_role_arns" {
  description = "Task role ARN per phase, the principal a workspace run role trusts"
  value       = module.runner.task_role_arns
}

output "ecr_repository_urls" {
  description = "ECR repository URL per image, for the deploy workflows"
  value       = module.registry.repository_urls
}

output "app_secret_arn" {
  description = "ARN of the single JSON app secret the control plane Lambdas read at cold start"
  value       = module.app_secrets.arns["app"]
}

output "runner_vpc_id" {
  description = "Id of the public VPC the runner tasks launch into"
  value       = module.vpc.vpc_id
}

output "staging_access_gate_hosted_ui" {
  description = "Cognito hosted UI base URL of the staging access gate, null when the gate is off"
  value       = one(module.staging_access_gate[*].hosted_ui_domain)
}

output "e2e_gate_signing_key_ssm_parameter_name" {
  description = "SSM SecureString holding the gate's CloudFront cookie signing key, set as the E2E_GATE_SIGNING_KEY_SSM_PARAMETER environment variable. Null when the gate is off"
  value       = one(module.staging_access_gate[*].signing_key_ssm_parameter_name)
}

output "e2e_gate_key_pair_id" {
  description = "CloudFront public key id the gate trusts, set as the E2E_GATE_KEY_PAIR_ID environment variable. Null when the gate is off"
  value       = one(module.staging_access_gate[*].signing_key_pair_id)
}

output "e2e_gate_cookie_domain" {
  description = "Domain the gate's signed cookies are scoped to, set as the E2E_GATE_COOKIE_DOMAIN environment variable. Null when the gate is off"
  value       = one(module.staging_access_gate[*].cookie_domain)
}

output "run_confirmations_queue_url" {
  description = "URL of the queue the state machine sends a confirmation request to, carrying the task token the runs domain resumes the execution with"
  value       = module.run_confirmations.queue_url
}

output "example_run_role_arn" {
  description = "ARN of the example run role, set as run_role_arn on the example workspace. Null when example_workspace_id is empty"
  value       = one(aws_iam_role.example_run_role[*].arn)
}

output "e2e_run_role_arn" {
  description = "ARN of the run role the e2e suite passes when it creates a workspace. Null outside staging"
  value       = one(aws_iam_role.e2e_run_role[*].arn)
}

output "vcs_upload_url" {
  description = "Endpoint the reusable upload workflow calls with a GitHub Actions OIDC token to get a presigned ingest upload"
  value       = "${local.api_url}/api/v1/vcs/uploads"
}

output "vcs_oidc_audience" {
  description = "Audience the reusable upload workflow must request its GitHub Actions OIDC token with"
  value       = var.vcs_oidc_audience
}

output "vcs_ingest_queue_url" {
  description = "Queue EventBridge feeds with Object Created events under ingest/ in the artifacts bucket"
  value       = module.vcs_ingest.queue_url
}
