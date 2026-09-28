"""The workspace reads both deployed functions perform.

Not a deployable domain: the workspaces function owns every write to these tables
and serves their routes, while the runs function only reads them. The reads, and
the exceptions they raise, live here rather than in either domain because one
Lambda image is deployed per domain, so a domain package that imports another
drags the whole of it into an image that was never granted anything to run it
against.

This sits under `app/common/` rather than `app/domains/` for the reason
`app/common/identity/` does: deploy attribution reaches a file under `app/common/`
through the import closure of each domain entrypoint, so a change here redeploys
every function that reaches it, while a package under `app/domains/` without an
entrypoint is no domain and attributes to nothing.

Nothing here writes. The runs role holds a read only grant on the workspaces,
variables and config-versions tables, so a write reached from this package would
be denied under that role at runtime. The `uploaded` flip on a config version row
stays in the workspaces domain, which owns it.
"""
