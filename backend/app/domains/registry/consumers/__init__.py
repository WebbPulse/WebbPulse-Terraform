"""Queue consumers the registry domain's own image runs.

`dispatch` owns the adapter's pass-through route and routes each record on its
body's `kind`; `ingest` validates an uploaded module tarball and publishes it.
"""
