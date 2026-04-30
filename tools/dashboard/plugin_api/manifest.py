"""Pydantic schema for ``plugin.yaml`` manifests.

A manifest is data, not code. The loader can enumerate and validate
manifests without importing any plugin Python — entrypoints carry
``module:attr`` strings that are only resolved for plugins that survive
the enable filter.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


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


class PluginFrontend(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alpine_root: str


class PluginEntrypoints(BaseModel):
    """All entrypoint fields are independent and optional."""
    model_config = ConfigDict(extra="forbid")
    api: Optional[str] = None
    badge_counter: Optional[str] = None
    schemas: Optional[List[str]] = None


class PluginManifest(BaseModel):
    """Validated shape of a ``plugin.yaml`` file."""
    model_config = ConfigDict(extra="forbid")

    id: str
    api_version: int
    paths: List[str] = Field(min_length=1)
    assets: PluginAssets
    nav: PluginNav
    frontend: PluginFrontend
    entrypoints: PluginEntrypoints = Field(default_factory=PluginEntrypoints)
    # Bootstrap default for when no `dashboard.plugin#1` Setting row
    # exists yet. Plugins that should ship dormant (operator opts in)
    # set this to false; the directory-name convention (``_``-prefix)
    # still applies as a fallback so existing samples keep working.
    default_enabled: Optional[bool] = None
