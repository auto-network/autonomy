"""Dashboard plugin substrate.

A plugin is a directory under ``tools/dashboard/plugins/<id>/`` with a
``plugin.yaml`` manifest. The substrate discovers plugins, filters by
the ``dashboard.plugin#1`` Setting, resolves any declared entrypoints,
and exposes the resulting registry to the dashboard server.

See bead auto-a79f6 + design note ``graph://f77a5415-04f``.
"""

from .manifest import PluginManifest
from .schema import DashboardPluginV1, PLUGIN_SET_ID, PLUGIN_SCHEMA_REVISION

__all__ = [
    "PluginManifest",
    "DashboardPluginV1",
    "PLUGIN_SET_ID",
    "PLUGIN_SCHEMA_REVISION",
]
