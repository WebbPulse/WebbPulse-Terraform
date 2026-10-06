"""Cutover tooling for moving HCP Terraform workspaces onto the owned control plane.

Implements steps 1 to 4 and the state rollback of the Phase 6 cutover runbook in
TFC-REPLACEMENT.md. State bodies are only ever held in memory or in a private
directory that is shredded on exit, and nothing derived from them is printed beyond
serial, lineage and engine version.
"""
