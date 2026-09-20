module "registry" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/ecr-repository"
  version = "~> 2.27"

  name_prefix = local.project

  keep_last_tagged_images = 3

  repositories = {
    workspaces = {}
    runs       = {}
    runner     = {}
  }
}
