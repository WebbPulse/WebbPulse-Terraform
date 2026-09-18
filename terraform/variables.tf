variable "aws_region" {
  description = "AWS region to deploy resources into"
  type        = string
  default     = "us-west-2"
}

variable "environment" {
  description = "Deployment environment (production, staging)"
  type        = string
  default     = "production"

  validation {
    condition     = contains(["production", "staging"], var.environment)
    error_message = "environment must be 'production' or 'staging'"
  }
}

variable "staging_profile" {
  description = "How much of the stack this environment provisions. 'none' switches the environment off."
  type        = string
  default     = "full"

  validation {
    condition     = contains(["none", "reduced", "full"], var.staging_profile)
    error_message = "staging_profile must be one of 'none', 'reduced', or 'full'."
  }

  validation {
    condition     = var.staging_profile != "none"
    error_message = "Refusing to plan: staging_profile is 'none', so this environment is switched off and no resources should be created in it. To stand this environment up, change staging_profile to 'reduced' or 'full' on the workspace in WebbPulse-Organization/bootstrap/locals.tf."
  }
}

variable "route53_zone_id" {
  description = "Route53 hosted zone ID of the parent zone (webbpulse.com), owned by the management account. Both environments write their hostname into it through route53_write_role_arn."
  type        = string
  default     = null

  validation {
    condition     = var.staging_profile != "full" || var.route53_zone_id != null
    error_message = "route53_zone_id must be set when staging_profile is 'full'. The webbpulse.com hosted zone is owned by the WebbPulse-Organization bootstrap workspace; set the workspace variable from WebbPulse-Organization/bootstrap/locals.tf."
  }
}

variable "route53_write_role_arn" {
  description = "IAM role ARN in the management account assumed to write this environment's records into the parent zone. Empty means write with the run role directly."
  type        = string
  default     = ""

  validation {
    condition     = var.staging_profile != "full" || var.route53_write_role_arn != ""
    error_message = "route53_write_role_arn must be set when staging_profile is 'full': the hostname records live in the parent zone in the management account, which the run role cannot write directly."
  }
}

variable "staging_access_gate" {
  description = "Put the staging site and API behind the staging-access-gate module. Staging only; production keeps the default of false."
  type        = bool
  default     = false
}

variable "staging_access_users" {
  description = "Email addresses allowed through the staging access gate, each invited as a Cognito user"
  type        = list(string)
  default     = []
}

variable "bootstrap_image_tag" {
  description = "Image tag seeding every per-domain function at create time. It must already exist in the workspaces and runs ECR repositories; image_uri is ignored thereafter, so deploys own it."
  type        = string
  default     = "bootstrap"
}

variable "runner_image_tag" {
  description = "Image tag the plan and apply task definitions point at. Unlike a Lambda image this is not under ignore_changes, so a task definition revision follows this value."
  type        = string
  default     = "bootstrap"
}

variable "runner_task_cpu" {
  description = "Fargate CPU units for the runner task, as one of the strings ECS accepts"
  type        = string
  default     = "1024"
}

variable "runner_task_memory" {
  description = "Fargate memory in MiB for the runner task, as one of the strings ECS accepts for runner_task_cpu"
  type        = string
  default     = "2048"
}

variable "vpc_cidr_block" {
  description = "IPv4 CIDR block of the runner VPC. Changing it renumbers and therefore replaces every subnet."
  type        = string
  default     = "10.40.0.0/16"
}

variable "artifact_retention_days" {
  description = "Days before a config tarball, plan artifact or phase log in the artifacts bucket expires"
  type        = number
  default     = 90
}

variable "identity_jwt_mode" {
  description = "Which mechanism enforces identity access tokens at the gateway: the staging gate's Lambda authorizer (gate), API Gateway's own JWT authorizer (native), or nothing (off)."
  type        = string
  default     = "off"

  validation {
    condition     = contains(["native", "gate", "off"], var.identity_jwt_mode)
    error_message = "identity_jwt_mode must be one of native, gate or off."
  }

  validation {
    condition     = var.identity_jwt_mode != "native" || var.environment != "staging"
    error_message = "identity_jwt_mode must not be native in staging. Every route there carries the staging access gate's REQUEST authorizer and a route takes exactly one authorizer, so a native JWT authorizer has no slot to occupy. Use gate, which moves the same check into the gate's own Lambda."
  }
}
