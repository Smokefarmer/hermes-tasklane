"""Standalone TaskLane — autonomous coding-job control plane.

Extracted from the Hermes agent so coding jobs can be driven remotely (e.g. from
a laptop's Claude over MCP) without depending on the Hermes gateway. Jobs execute
via the Claude Code CLI (`claude -p`) on the user's subscription.
"""

__version__ = "0.2.0"
