"""The HCP Terraform API surface (`tfe.v2`) the `cloud {}` block and go-tfe speak.

Not a deployable domain. The workspaces and runs functions each serve part of
`/api/v2`, and both render the same JSON:API resources, so the shapes, the error
envelope and the access checks live here where neither image has to import the other.
"""
