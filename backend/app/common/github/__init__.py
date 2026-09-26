"""The GitHub App settings loader every domain reads credentials through.

The client itself is `webbpulse.integrations.github`; this package adds only the short
TTL cache in `loader`, so a warm function sees credentials written after its cold start.
"""
