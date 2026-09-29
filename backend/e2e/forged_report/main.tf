terraform {
  required_version = ">= 1.10"

  required_providers {
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3"
    }
  }
}

variable "api_base_url" {
  type = string
}

data "external" "forge" {
  program = ["python3", "${path.module}/forge.py"]

  query = {
    api_base_url = var.api_base_url
  }
}

resource "terraform_data" "real" {
  input = data.external.forge.result["attempted"]
}

output "forge" {
  value = data.external.forge.result
}
