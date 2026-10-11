identity_jwt_mode = "gate"

access_gate       = true
access_gate_users = ["tylert2610@gmail.com", "tyler@webbpulse.com"]

oidc_issuer_enabled = true

external_run_role_arns = [
  "arn:aws:iam::488386929690:role/WebbPulse-Platform-Terraform",
  "arn:aws:iam::488386929690:role/WebbPulse-Organization-Terraform",
  "arn:aws:iam::734702670403:role/CarModPicker-Terraform",
  "arn:aws:iam::748861776298:role/CarModPicker-staging-Terraform",
  "arn:aws:iam::036807648992:role/WebbPulse-Terraform",
  "arn:aws:iam::621554169154:role/WebbPulse-Portfolio-staging-Terraform",
  "arn:aws:iam::432410731887:role/WebbPulse-Artifacts-Terraform",
  "arn:aws:iam::897427573432:role/WebbPulse-Terraform-Terraform",
  "arn:aws:iam::870550636948:role/WebbPulse-Terraform-staging-Terraform",
  "arn:aws:iam::147741822161:role/Standupless-Terraform",
  "arn:aws:iam::212598081999:role/Standupless-staging-Terraform",
  "arn:aws:iam::488386929690:role/WebbPulse-Platform-Terraform-Plan",
  "arn:aws:iam::488386929690:role/WebbPulse-Organization-Terraform-Plan",
  "arn:aws:iam::734702670403:role/CarModPicker-Terraform-Plan",
  "arn:aws:iam::748861776298:role/CarModPicker-staging-Terraform-Plan",
  "arn:aws:iam::036807648992:role/WebbPulse-Terraform-Plan",
  "arn:aws:iam::621554169154:role/WebbPulse-Portfolio-staging-Terraform-Plan",
  "arn:aws:iam::432410731887:role/WebbPulse-Artifacts-Terraform-Plan",
  "arn:aws:iam::897427573432:role/WebbPulse-Terraform-Terraform-Plan",
  "arn:aws:iam::870550636948:role/WebbPulse-Terraform-staging-Terraform-Plan",
  "arn:aws:iam::147741822161:role/Standupless-Terraform-Plan",
  "arn:aws:iam::212598081999:role/Standupless-staging-Terraform-Plan",
]
