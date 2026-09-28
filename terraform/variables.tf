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

variable "access_gate" {
  description = "Put the site and API behind the access gate: a Cognito sign-in wall at CloudFront and the gate's REQUEST authorizer on every API route, in front of the application's own login, step-up and route scopes. Set in env/<environment>.tfvars. Needs staging_profile full and route53_zone_id."
  type        = bool
  default     = false
}

variable "access_gate_users" {
  description = "Email addresses allowed through the access gate, each invited as a Cognito user. Set in env/<environment>.tfvars and merged after staging_access_users."
  type        = list(string)
  default     = []
}

variable "staging_access_gate" {
  description = "The access gate switch the WebbPulse-Platform workspace factory delivers to staging workspaces. Either this or access_gate turns the gate on."
  type        = bool
  default     = false
}

variable "staging_access_users" {
  description = "The access gate allow list the WebbPulse-Platform workspace factory delivers to staging workspaces, merged ahead of access_gate_users."
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

variable "domain_image_tags" {
  description = "Per domain image tags that override bootstrap_image_tag at create time, keyed by domain name. A domain declared with its own image tag, today only github, is left out of the function map until it has an entry here, because its ECR repository holds no image until the first deploy after the apply that creates it. image_uri is ignored after create, so deploys own it."
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for tag in values(var.domain_image_tags) : can(regex("^sha-[0-9a-f]{40}$", tag))])
    error_message = "Every domain_image_tags value must be sha- followed by a full 40 character commit sha, which is the tag the container image build pushes."
  }
}

variable "github_app_slug" {
  description = "Fallback slug of the operator owned GitHub App, passed to the Lambdas as GITHUB_APP_SLUG. The slug the manifest flow stores in the github table wins over it, so leave it empty unless an App was created outside that flow."
  type        = string
  default     = ""
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
  description = "Which mechanism enforces identity access tokens at the gateway: the access gate's Lambda authorizer (gate), a REQUEST authorizer built from the same source that also passes wpk_ agent keys through (lambda), API Gateway's own JWT authorizer (native), or nothing (off). With the access gate on it must be gate; production without the gate must use lambda or native."
  type        = string
  default     = "off"

  validation {
    condition     = contains(["lambda", "native", "gate", "off"], var.identity_jwt_mode)
    error_message = "identity_jwt_mode must be one of lambda, native, gate or off."
  }

  validation {
    condition     = !contains(["native", "lambda"], var.identity_jwt_mode) || var.environment != "staging"
    error_message = "identity_jwt_mode must not be native or lambda in staging. Every route there carries the staging access gate's REQUEST authorizer and a route takes exactly one authorizer, so a second authorizer has no slot to occupy. Use gate, which moves the same check into the gate's own Lambda."
  }

  validation {
    condition     = !contains(["native", "lambda"], var.identity_jwt_mode) || !(var.access_gate || var.staging_access_gate)
    error_message = "identity_jwt_mode must not be native or lambda while the access gate is on. Every route carries the gate's REQUEST authorizer and a route takes exactly one authorizer, so a second authorizer has no slot to occupy. Use gate, which moves the same check into the gate's own Lambda."
  }

  validation {
    condition = var.environment != "production" || contains(["lambda", "native"], var.identity_jwt_mode) || (
      var.identity_jwt_mode == "gate" && (var.access_gate || var.staging_access_gate) && var.staging_profile == "full" && var.route53_zone_id != null
    )
    error_message = "identity_jwt_mode must be gate with the access gate on, or lambda or native without it, in production. off, and gate without a working access gate (access_gate true, staging_profile full and route53_zone_id set), leave every product route open at the gateway."
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

variable "oidc_issuer_enabled" {
  description = "Serve the control plane's OIDC issuer on oidc.<host> and let the runs function sign workload identity tokens for Google and Azure. Needs the custom domains; in production the Platform Route 53 writer must also list oidc.terraform.webbpulse.com before it can be true"
  type        = bool
  default     = true
}

variable "oidc_signing_key_generations" {
  description = "Labels of the RSA signing keys the issuer publishes, oldest first. The last one signs. Rotate by appending a label and applying, which publishes the new key and switches signing to it, then drop the old label once every token it signed has expired (an hour)"
  type        = list(string)
  default     = ["1"]

  validation {
    condition     = length(var.oidc_signing_key_generations) > 0 && length(distinct(var.oidc_signing_key_generations)) == length(var.oidc_signing_key_generations)
    error_message = "oidc_signing_key_generations must list at least one label, each once."
  }

  validation {
    condition     = alltrue([for label in var.oidc_signing_key_generations : can(regex("^[a-z0-9-]{1,16}$", label))])
    error_message = "Each oidc_signing_key_generations label must be 1 to 16 lowercase letters, digits or hyphens."
  }
}
