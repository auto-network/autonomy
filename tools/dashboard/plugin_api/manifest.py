"""Pydantic schema for ``plugin.yaml`` manifests.

A manifest is data, not code. The loader can enumerate and validate
manifests without importing any plugin Python — entrypoints carry
``module:attr`` strings that are only resolved for plugins that survive
the enable filter.
"""
from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


# The substrate API version this dashboard supports. Plugins declare the
# version they target; mismatched versions are skipped at discovery.
SUBSTRATE_API_VERSION = 1


class PluginAssets(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template: str
    script: str
    style: Optional[str] = None


class PluginNav(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    # A plugin may belong in a contextual shell launcher instead of the
    # always-visible sidebar.  The shell consumes these generic hints; it
    # never needs to know which product plugin supplied them.
    sidebar: bool = True
    identity_menu: bool = False
    identity_detail: Optional[str] = None


class PluginFrontend(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alpine_root: str


class PluginCapability(BaseModel):
    """Capability implementation whose deterministic UI this plugin is."""

    model_config = ConfigDict(extra="forbid")
    contract: str = Field(min_length=1)
    implementation: str = Field(min_length=1)


class PluginEntrypoints(BaseModel):
    """All entrypoint fields are independent and optional."""
    model_config = ConfigDict(extra="forbid")
    api: Optional[str] = None
    badge_counter: Optional[str] = None
    # Optional batched callback for session-card / session-viewer chrome.
    # Signature: ``(session_ids: list[str], request: Request) ->
    # dict[str, list[descriptor]]``. The plugin owns relationship lookup and
    # authorization; the substrate only normalizes and renders descriptors.
    session_contributions: Optional[str] = None
    schemas: Optional[List[str]] = None
    # Each entry is a bare module path — the loader imports it for the
    # ``register_action(...)`` side effect at module top-level. No attribute
    # is resolved; the registry is the contract.
    actions: Optional[List[str]] = None


class PluginSettingDeclaration(BaseModel):
    """A graph Setting bundled with a plugin and owned by its lifecycle."""
    model_config = ConfigDict(extra="forbid")

    set_id: str
    schema_revision: int
    key: str
    state: str = "canonical"
    payload: Optional[dict[str, Any]] = None
    payload_file: Optional[str] = None
    uninstall: str = "deprecate_if_unchanged"

    @model_validator(mode="after")
    def _validate_payload_source(self):
        if (self.payload is None) == (self.payload_file is None):
            raise ValueError("exactly one of payload or payload_file is required")
        if self.state not in {"raw", "curated", "published", "canonical"}:
            raise ValueError("state must be raw, curated, published, or canonical")
        if self.uninstall not in {"deprecate_if_unchanged", "leave"}:
            raise ValueError("uninstall must be deprecate_if_unchanged or leave")
        return self


class PluginManifest(BaseModel):
    """Validated shape of a ``plugin.yaml`` file."""
    model_config = ConfigDict(extra="forbid")

    id: str
    api_version: int
    # Default install scope. The substrate reads this plugin's
    # ``dashboard.plugin#1`` toggle row from ``<org>.db``. Operators
    # override per-installation by writing a payload with ``org: <slug>``.
    org: str
    paths: List[str] = Field(min_length=1)
    assets: PluginAssets
    nav: PluginNav
    frontend: PluginFrontend
    # Optional direct link from a deterministic dashboard projection to the
    # workspace capability that supplies its agent-facing tools and primer.
    capability: Optional[PluginCapability] = None
    entrypoints: PluginEntrypoints = Field(default_factory=PluginEntrypoints)
    # Graph Settings installed/reconciled as part of plugin lifecycle.
    settings: List[PluginSettingDeclaration] = Field(default_factory=list)
    # Bootstrap default for when no `dashboard.plugin#1` Setting row
    # exists yet. Plugins that should ship dormant (operator opts in)
    # set this to false; the directory-name convention (``_``-prefix)
    # still applies as a fallback so existing samples keep working.
    default_enabled: Optional[bool] = None
    # Optional agent-facing documentation, path relative to the plugin
    # directory (convention: SKILL.md). When set, the file is embedded
    # into the workspace primer's "Dashboard Apps" section for every
    # session while the plugin is enabled, and served at
    # GET /api/plugins/{id}/skill.
    skill: Optional[str] = None
