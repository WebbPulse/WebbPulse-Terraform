locals {
  dynamodb_tables = {
    workspaces = {
      hash_key = "workspace_id"
      attributes = [
        { name = "workspace_id", type = "S" },
        { name = "name", type = "S" },
      ]
      global_secondary_indexes = [
        { name = "by_name", hash_key = "name", projection_type = "ALL" },
      ]
    }

    runs = {
      hash_key = "run_id"
      attributes = [
        { name = "run_id", type = "S" },
        { name = "workspace_id", type = "S" },
        { name = "created_at", type = "S" },
        { name = "collection", type = "S" },
      ]
      global_secondary_indexes = [
        { name = "by_workspace", hash_key = "workspace_id", range_key = "created_at", projection_type = "ALL" },
        { name = "by_recency", hash_key = "collection", range_key = "run_id", projection_type = "ALL" },
      ]
    }

    variables = {
      hash_key  = "workspace_id"
      range_key = "key"
      attributes = [
        { name = "workspace_id", type = "S" },
        { name = "key", type = "S" },
      ]
    }

    "config-versions" = {
      hash_key = "config_version_id"
      attributes = [
        { name = "config_version_id", type = "S" },
        { name = "workspace_id", type = "S" },
        { name = "created_at", type = "S" },
      ]
      global_secondary_indexes = [
        { name = "by_workspace", hash_key = "workspace_id", range_key = "created_at", projection_type = "ALL" },
      ]
    }

    users = {
      hash_key = "id"
      attributes = [
        { name = "id", type = "S" },
        { name = "email_lower", type = "S" },
      ]
      global_secondary_indexes = [
        { name = "email_lower-index", hash_key = "email_lower", projection_type = "ALL" },
      ]
    }

    github = {
      hash_key      = "pk"
      range_key     = "sk"
      ttl_attribute = "expires_at"
      attributes = [
        { name = "pk", type = "S" },
        { name = "sk", type = "S" },
      ]
    }
  }
}

module "dynamodb" {
  source  = "app.terraform.io/WebbPulse/platform-modules/aws//modules/dynamodb-tables"
  version = "~> 2.27"

  name_prefix = local.prefix
  tables      = local.dynamodb_tables

  point_in_time_recovery = true
  deletion_protection    = var.environment == "production"
}
