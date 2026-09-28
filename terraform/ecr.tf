module "registry" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/ecr-repository"
  version = "~> 2.32"

  name_prefix = local.project

  keep_last_tagged_images = 3

  repositories = {
    workspaces = {}
    runs       = {}
    github     = {}
    registry   = {}
    runner = {
      image_tag_mutability = "MUTABLE"
    }
  }
}
