"""Queue consumers the runs domain's own image runs.

`dispatch` owns the one route they share, because the Lambda Web Adapter posts every
queue invocation to a single pass-through path; each other module here is a
`handle_record` it routes to on the body's `kind`.
"""
