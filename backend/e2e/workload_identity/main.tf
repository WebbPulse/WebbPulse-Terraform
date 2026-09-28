terraform {
  required_version = ">= 1.11"

  required_providers {
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3"
    }
  }
}

variable "issuer" {
  type = string
}

variable "workspace_id" {
  type = string
}

data "external" "workload_identity" {
  program = ["python3", "${path.module}/verify.py"]

  query = {
    issuer       = var.issuer
    workspace_id = var.workspace_id
  }
}

output "workload_identity" {
  value = data.external.workload_identity.result
}
