"""What the workspaces function needs from the runs table without the runs domain.

Not a deployable domain. Deleting a workspace has to know whether a run on it is
still going and has to remove its finished runs, and one Lambda image is deployed
per domain, so importing the runs domain for that would drag it whole into the
workspaces image. This sits under `app/common/` for the same attribution reason as
`app/common/workspaces/`.
"""
