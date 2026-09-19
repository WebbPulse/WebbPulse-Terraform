"""The identity glue the workspaces function mounts.

Not a deployable domain of its own: the workspaces function serves `/api/auth`, so
there is no entrypoint here and no entry in `wiring.DOMAINS`. It lives under
`app/common/` rather than `app/domains/` because deploy attribution reaches a file
under `app/common/` through the import closure of each domain entrypoint, while a
package under `app/domains/` without an entrypoint is no domain and attributes to
nothing, so a change here would ship no image.

Importing this package imports no AWS client; the glue does its own imports in
function bodies, which is what keeps the runs image free of it.
"""
