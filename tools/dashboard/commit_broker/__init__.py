"""Broker publish + signed-commit assembly (DN5).

Host-side / trusted-broker code. No agent process ever touches a credential,
a private key, or signed-object bytes it did not produce. See design note
``9eb199d8-c23`` (DN5) for the full spec.
"""
