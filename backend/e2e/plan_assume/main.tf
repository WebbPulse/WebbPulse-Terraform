terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

variable "webbpulse_run_phase" {
  type      = string
  default   = "apply"
  ephemeral = true
}

variable "reader_role_arn" {
  type = string
}

variable "writer_role_arn" {
  type = string
}

provider "aws" {
  region = "us-west-2"

  assume_role {
    role_arn = var.webbpulse_run_phase == "plan" ? var.reader_role_arn : var.writer_role_arn
  }
}

data "aws_caller_identity" "assumed" {}

output "assumed_arn" {
  value = data.aws_caller_identity.assumed.arn
}
