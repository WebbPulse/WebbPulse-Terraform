"""GitHub pieces shared across domains.

`loader` holds the GitHub App settings behind a short TTL cache, so a warm function
sees credentials written after its cold start; the client itself is
`webbpulse.integrations.github`. `oidc` verifies the GitHub Actions OIDC token a
workflow authenticates with.
"""
