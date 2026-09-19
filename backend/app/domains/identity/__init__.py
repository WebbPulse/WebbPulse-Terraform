"""The identity glue the workspaces function mounts.

Not a deployable domain of its own: the workspaces function serves `/api/auth`,
so there is no entrypoint here and no entry in `wiring.DOMAINS`. Importing this
package imports no AWS client; the glue does its own imports in function bodies,
which is what keeps the runs image free of it.
"""
