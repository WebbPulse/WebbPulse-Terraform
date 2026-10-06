terraform {
  required_providers {
    archive = {
      source = "hashicorp/archive"
    }
  }
}

data "archive_file" "handler" {
  type        = "zip"
  output_path = "${path.module}/.build/handler.zip"

  source {
    content  = templatefile("${path.module}/handler.js.tftpl", { name = "carried" })
    filename = "index.js"
  }
}

output "output_path" {
  value = data.archive_file.handler.output_path
}
