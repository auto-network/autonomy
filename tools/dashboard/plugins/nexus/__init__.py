"""Settings Nexus dashboard plugin (bead auto-ct3ey).

A journal/timeline operator surface backed by two Settings:

* ``dashboard.nexus.scene`` (singleton, key=``active``) — the page
  banner: title, subtitle, anchor sentence, presenter pointer.
* ``dashboard.nexus.tile`` (keyed-per-entity) — color-coded entries
  in the timeline. Each tile carries a ``kind`` discriminator and an
  open-ended ``data`` payload for kind-specific fields (per the
  Design Studio fixture).

This is the first consumer of the Nexus presentation substrate
(scene + tile schemas). Subsequent beads layer in Presence wiring,
live pulse counters, and substrate explorer affordances.
"""
