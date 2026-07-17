"""auto.network registry service v1.

FastAPI + SQLite implementation of spec §4 (graph note ``a17c8657-939``):
org bindings, share-link grants, rebind policy, revocations. Every
mutation is authorized by an idkit delegation chain verifying to the org
binding's root key — the registry holds no permission tables (I4).

Bead: ``auto-4p7bg`` (B1). Depends on idkit (``tools.network.idkit``, A1).
"""

from .app import create_app
from .signing import REQUEST_DOMAIN, sign_request

__all__ = ["create_app", "sign_request", "REQUEST_DOMAIN"]
