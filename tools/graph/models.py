"""Core data models for the Autonomy Knowledge Graph."""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4


def new_id() -> str:
    return str(uuid4())


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Source:
    id: str = field(default_factory=new_id)
    type: str = "conversation"
    platform: str | None = None
    project: str | None = None
    title: str | None = None
    url: str | None = None
    file_path: str | None = None
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    ingested_at: str = field(default_factory=now_iso)
    last_activity_at: str | None = None
    publication_state: str = "raw"        # raw | curated | published | canonical — see graph://8cf067e3-ca3
    deprecated: bool = False
    successor_id: str | None = None


@dataclass
class Thought:
    id: str = field(default_factory=new_id)
    source_id: str = ""
    content: str = ""
    role: str = "user"
    turn_number: int | None = None
    message_id: str | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)


@dataclass
class Derivation:
    id: str = field(default_factory=new_id)
    source_id: str = ""
    thought_id: str | None = None
    content: str = ""
    model: str | None = None
    turn_number: int | None = None
    message_id: str | None = None
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)


@dataclass
class Entity:
    id: str = field(default_factory=new_id)
    name: str = ""
    canonical_name: str = ""
    type: str = "concept"
    description: str | None = None
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)


@dataclass
class Claim:
    id: str = field(default_factory=new_id)
    subject_id: str = ""
    predicate: str = ""
    object_id: str | None = None
    object_val: str | None = None
    source_id: str | None = None
    asserted_by: str = "user"
    confidence: float = 1.0
    status: str = "asserted"
    evidence: str | None = None
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)


@dataclass
class Edge:
    id: str = field(default_factory=new_id)
    source_id: str = ""
    source_type: str = ""
    target_id: str = ""
    target_type: str = ""
    relation: str = ""
    weight: float = 1.0
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)


@dataclass
class Attachment:
    id: str = field(default_factory=new_id)
    hash: str = ""
    filename: str = ""
    mime_type: str | None = None
    size_bytes: int = 0
    file_path: str = ""
    source_id: str | None = None
    turn_number: int | None = None
    metadata: dict = field(default_factory=dict)
    alt_text: str | None = None
    created_at: str = field(default_factory=now_iso)


@dataclass
class Node:
    """Hierarchical knowledge tree node."""
    id: str = field(default_factory=new_id)
    parent_id: str | None = None
    type: str = "component"
    title: str = ""
    description: str | None = None
    status: str = "active"
    sort_order: int = 0
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
