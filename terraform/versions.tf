terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.46"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }

  cloud {
    organization = "WebbPulse"

    workspaces {
      name = "WebbPulse-Terraform"
    }
  }
}
