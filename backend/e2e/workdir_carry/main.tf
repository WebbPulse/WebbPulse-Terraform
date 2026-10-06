terraform {
  required_version = ">= 1.10"

  required_providers {
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.7"
    }
  }
}

module "bundle" {
  source = "./bundle"
}

resource "terraform_data" "handler" {
  input = filesha256(module.bundle.output_path)
}

output "handler_sha256" {
  value = terraform_data.handler.output
}
