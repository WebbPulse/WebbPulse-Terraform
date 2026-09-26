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
  description = "Route53 hosted zone ID of the parent zone (webbpulse.com), owned by the management account. Production writes its hostname records into it through route53_write_role_arn; staging writes only the NS delegation of its child zone."
  type        = string
  default     = null

  validation {
    condition     = var.staging_profile != "full" || var.route53_zone_id != null
    error_message = "route53_zone_id must be set when staging_profile is 'full'. The webbpulse.com hosted zone is owned by the WebbPulse-Organization bootstrap workspace; set the workspace variable from WebbPulse-Organization/bootstrap/locals.tf."
  }
}

variable "route53_write_role_arn" {
  description = "IAM role ARN in the management account assumed to write into the parent zone: the hostname records in production, the child zone NS delegation in staging. Empty means write with the run role directly."
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
  description = "Image tag every per-domain function is seeded from at create time. Lambda resolves the tag during CreateFunction, so it must already exist in the workspaces and runs ECR repositories before the apply. The empty string resolves the domain map to empty, which is how a fresh account applies once with no images in ECR. image_uri is ignored after create, so deploys own it."
  type        = string
  default     = ""

  validation {
    condition     = var.bootstrap_image_tag == "" || can(regex("^sha-[0-9a-f]{40}$", var.bootstrap_image_tag))
    error_message = "bootstrap_image_tag must be sha- followed by a full 40 character commit sha, which is the tag the container image build pushes, or the empty string to bootstrap an account whose ECR repositories hold no images yet."
  }
}

variable "runner_image_tag" {
  description = "Pin the plan and apply task definitions to one runner image tag, for example sha-<commit>. Leave it null, the default, and the task definitions follow the environment tag that Deploy Runner moves on every deploy, which is the normal path. Set it only to hold the runner on a known image; unlike a Lambda image this is not under ignore_changes, so a task definition revision follows this value."
  type        = string
  default     = null
  nullable    = true
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

variable "example_workspace_id" {
  description = "Workspace id of the example workspace used for the first end to end run. Non-empty creates the example run role, whose trust condition and state object key are both derived from this id. Empty, the default, creates nothing."
  type        = string
  default     = ""

  validation {
    condition     = var.example_workspace_id == "" || can(regex("^ws-[0-9A-HJKMNP-TV-Z]{26}$", var.example_workspace_id))
    error_message = "example_workspace_id must be a workspace id: ws- followed by a 26 character ULID, or the empty string."
  }
}

variable "adopt_spans_log_group" {
  description = "Adopt the reserved aws/spans log group into state and hold it at 7 day retention. X-Ray creates that group itself the first time it writes a span to the CloudWatchLogs destination, and it cannot be created ahead of time because CreateLogGroup rejects names beginning with aws/. An import block whose target does not exist is a plan time error, so a new account applies once with this false, generates one span, then sets the workspace variable true and applies again. Neither account has written a span yet, so the default is false."
  type        = bool
  default     = false
}

variable "vcs_oidc_audience" {
  description = "Audience a GitHub Actions OIDC token must carry for POST /api/v1/vcs/uploads to accept it. The reusable upload workflow requests its token with this audience."
  type        = string
  default     = "webbpulse-terraform"

  validation {
    condition     = length(trimspace(var.vcs_oidc_audience)) > 0
    error_message = "vcs_oidc_audience must not be empty."
  }
}
