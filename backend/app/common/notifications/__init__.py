"""Run notifications: workspace notification configurations and their deliveries.

Shared by the workspaces function, which stores the configurations and sends a test,
and the runs function, which turns run transitions into deliveries from its queue.
Modelled on HCP Terraform's notification configurations.
"""
