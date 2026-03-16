"""Seed the knowledge hierarchy from the Autonomy vision.

This builds the logical tree structure that agents use to orient
and work on subtasks. Derived from musings + conversations.
"""

from __future__ import annotations
from .models import Node
from .db import GraphDB


def seed_hierarchy(db: GraphDB) -> int:
    """Seed the full Autonomy knowledge tree. Returns node count."""
    count = 0

    def add(parent_id, type, title, description=None, status="planned", order=0):
        nonlocal count
        n = Node(parent_id=parent_id, type=type, title=title,
                 description=description, status=status, sort_order=order)
        db.insert_node(n)
        count += 1
        return n.id

    # ═══════════════════════════════════════════════════════════
    # ROOT: Mission
    # ═══════════════════════════════════════════════════════════
    root = add(None, "mission", "Autonomy Network",
               "User-sovereign protocol operating system for the networked world. "
               "Shared protocol, sovereign edge, optional infra.",
               status="active", order=0)

    # ═══════════════════════════════════════════════════════════
    # TIER 1: Core Architecture Modules
    # ═══════════════════════════════════════════════════════════

    # ── Autonomy Core ────────────────────────────────────────
    core = add(root, "module", "Autonomy Core",
               "Constitutional layer. Universal primitives, discovery, compatibility rules, "
               "event envelopes, capability model, policy hooks. Small and sacred.",
               order=1)

    core_id = add(core, "component", "Identity & Addressing",
                  "Actor identity, namespaces, cryptographic keying, authentication. "
                  "Global access and identity for each node in the network.", order=1)
    add(core_id, "feature", "Actor / Node Identity", "Cryptographic keys per node/user/agent", order=1)
    add(core_id, "feature", "Namespace System", "Hierarchical namespace allocation and resolution", order=2)
    add(core_id, "feature", "Authentication & Attestation", "Signed hashes, timestamps, vouches", order=3)
    add(core_id, "feature", "BlindHash Service", "Privacy-preserving identity lookup ($0.01/call)", order=4)

    core_obj = add(core, "component", "Object Model",
                   "Universal primitives: Node, Edge, Claim, Artifact, Event, Frontier. "
                   "Core defines how things are identified, versioned, claimed, trusted.", order=2)
    add(core_obj, "feature", "Node / Edge Primitives", "Typed objects with stable IDs and typed relationships", order=1)
    add(core_obj, "feature", "Claims & Provenance", "Structured assertions: subject→predicate→object with evidence, trust vector, status", order=2)
    add(core_obj, "feature", "Artifacts", "File/blob/media objects with identity", order=3)
    add(core_obj, "feature", "Events & Causal Ordering", "Immutable event log, causal DAG, version semantics", order=4)
    add(core_obj, "feature", "Frontiers & Revisions", "Named branches, version vectors, multi-head support", order=5)

    core_cap = add(core, "component", "Capabilities & Policy",
                   "Capability declaration, grants, policy hooks, trust primitives. "
                   "Cryptographically grantable and revocable.", order=3)
    add(core_cap, "feature", "Capability Model", "Scoped, grantable, revocable capability tokens", order=1)
    add(core_cap, "feature", "Policy Hooks", "Validation, trust, authorization logic", order=2)
    add(core_cap, "feature", "Trust Primitives", "Multi-dimensional trust: source, contributor, corroboration, procedural, contextual", order=3)

    core_coord = add(core, "component", "Coordination Fabric",
                     "Channels, workstreams, commitments, reviews, decisions, feature flags, deliveries. "
                     "How the network evolves.", order=4)
    add(core_coord, "feature", "Channels", "Distributed gossip streams with structured protocol envelopes", order=1)
    add(core_coord, "feature", "Workstreams", "Living containers binding intent→spec→prototype→review→delivery", order=2)
    add(core_coord, "feature", "Commitments", "Ownership + quality + time promises as protocol objects", order=3)
    add(core_coord, "feature", "Reviews & Decisions", "Human/automated evaluation, accept/reject/defer/supersede", order=4)
    add(core_coord, "feature", "Feature Flags", "Epistemic and operational branch controls, not just booleans", order=5)
    add(core_coord, "feature", "Deliveries", "Unit of rollout with maturity lifecycle", order=6)

    core_spec = add(core, "component", "Spec & Discovery",
                    "Root domain as constitutional surface. Human-readable + machine-readable. "
                    "Module registry, schema registry, capability registry.", order=5)
    add(core_spec, "feature", "Spec Publication", "Normative specs as governed objects with identity, versions, review state", order=1)
    add(core_spec, "feature", "Module Registry", "Discovery of domain modules with compatibility info", order=2)
    add(core_spec, "feature", "Schema Registry", "Type definitions, extension rules, version negotiation", order=3)

    core_rep = add(core, "component", "Replication & Gossip",
                   "Causal op-log, CRDT substrate, gossip contracts, event propagation. "
                   "The nervous system signal transport.", order=6)
    add(core_rep, "feature", "Causal Op-Log", "Operation-based replication with causal ordering", order=1)
    add(core_rep, "feature", "Gossip Protocol", "Distributed state propagation with structured envelopes", order=2)
    add(core_rep, "feature", "Subscription & Discovery", "How nodes find and follow state changes", order=3)

    # ── Autonomy Runtime ─────────────────────────────────────
    runtime = add(root, "module", "Autonomy Runtime",
                  "Per-user sovereign control plane. Keys, module activations, policies, "
                  "agent permissions, sync choices, provider bindings. The operator's law.",
                  order=2)

    rt_local = add(runtime, "component", "Local Sovereignty",
                   "Module enable/disable, version pinning, feature flags, local overrides, "
                   "forks, patch layers. User controls their realization of shared protocol.", order=1)
    add(rt_local, "feature", "Module Activation", "Enable/disable/pin domain modules locally", order=1)
    add(rt_local, "feature", "Local Policy Engine", "Trust rules, agent permissions, sync choices", order=2)
    add(rt_local, "feature", "Local Overrides & Forks", "Compatible local implementations, patch layers", order=3)
    add(rt_local, "feature", "Export & Migration", "Data portability, provider switching, local-first storage", order=4)

    rt_agent = add(runtime, "component", "Agent Management",
                   "Agent identity, capability grants, event log, human checkpoint model. "
                   "The minimal sovereignty substrate for safe agentic loops.", order=2)
    add(rt_agent, "feature", "Agent Identity", "Who/what is acting", order=1)
    add(rt_agent, "feature", "Agent Permissions", "Explicit capability grants per agent", order=2)
    add(rt_agent, "feature", "Audit Log", "What the agent did, when, with what authority", order=3)
    add(rt_agent, "feature", "Human Checkpoints", "When the operator gets to intervene", order=4)

    rt_provider = add(runtime, "component", "Provider Bindings",
                      "Delegation to infra services with scoped, revocable grants. "
                      "Sovereign delegation, not platform capture.", order=3)

    # ── Autonomy Modules ─────────────────────────────────────
    modules = add(root, "module", "Autonomy Modules",
                  "Domain protocol packages. Typed domain law: objects, relations, events, "
                  "capabilities, trust rules, invariants, extension points.",
                  order=3)

    mod_re = add(modules, "component", "/real-estate",
                 "First vertical. Asset graph, media artifacts, survey/GIS, provenance-rich claims. "
                 "Parcel, Structure, Unit, Listing, Survey, Valuation, MarketObservation.", order=1)
    add(mod_re, "feature", "Asset Graph", "Property as composable object graph, not a row", order=1)
    add(mod_re, "feature", "Media Model", "Photos, video, drone, with provenance", order=2)
    add(mod_re, "feature", "GIS Integration", "Parcel geometry, survey, zoning, flood layers", order=3)
    add(mod_re, "feature", "Market Data", "Valuations, comps, market observations", order=4)
    add(mod_re, "feature", "Trust Classes", "Licensed surveyor vs listing scrape vs owner assertion", order=5)

    mod_market = add(modules, "component", "/market",
                     "Automated bidding, transaction clearing, trust scoring. "
                     "Multi-dimensional bidding with self-organizing hierarchy.", order=2)

    mod_trade = add(modules, "component", "/trade",
                    "Trading operations, positions, quotes, settlements.", order=3)

    mod_comms = add(modules, "component", "/comms",
                    "Public posts, groups, DM, audio/video/chat. "
                    "Comms go directly into the knowledge graph.", order=4)

    # ── Autonomy Surface ─────────────────────────────────────
    surface = add(root, "module", "Autonomy Surface",
                  "The sheet of glass. Projection of runtime state, not the owner. "
                  "Generative UI from semantic descriptions via LLM→template→factory pipeline.",
                  order=4)

    surf_ui = add(surface, "component", "UI Template Language",
                  "High-level semantic descriptions consumed by LLM to produce rendering templates. "
                  "Prose-like context narrowing, not rigid schemas.", order=1)
    add(surf_ui, "feature", "Semantic UI Descriptions", "Natural language context narrowing for rendering", order=1)
    add(surf_ui, "feature", "Component Factory", "Pre-built themed component library, fast deterministic rendering", order=2)
    add(surf_ui, "feature", "Cross-Domain Coherence", "Standardized metadata enables consistent look across independent domains", order=3)
    add(surf_ui, "feature", "Data Binding & Events", "Template-level data flow connecting backend to frontend", order=4)

    surf_learn = add(surface, "component", "Collective Learning",
                     "Session logs drive self-learning loops. Pattern extraction (not content) "
                     "shared across users. Local runtime as privacy boundary.", order=2)
    add(surf_learn, "feature", "Pattern Extraction", "Abstract structural navigation patterns from sessions", order=1)
    add(surf_learn, "feature", "Cross-Domain Transfer", "~20 core UI patterns refined by all users in all domains", order=2)

    # ── Autonomy Infra ───────────────────────────────────────
    infra = add(root, "module", "Autonomy Infra",
                "Optional delegated infrastructure. Centralization allowed below sovereignty line. "
                "Verifiable, portable, transparent, revocable, replaceable.",
                order=5)
    add(infra, "component", "Gossip Relays", "High-availability event transport", order=1)
    add(infra, "component", "Encrypted Storage", "Remote storage with client-side encryption", order=2)
    add(infra, "component", "Search & Indexing", "Full-text and graph search services", order=3)
    add(infra, "component", "Model Inference", "AI execution clusters for LLM/agent work", order=4)
    add(infra, "component", "Media Processing", "Image, video, document processing", order=5)
    add(infra, "component", "GIS Services", "Geographic computation and overlay services", order=6)
    add(infra, "component", "Domain Data Feeds", "Market data, assessor records, etc.", order=7)

    # ═══════════════════════════════════════════════════════════
    # TIER 2: Knowledge & Corpus Management
    # ═══════════════════════════════════════════════════════════
    knowledge = add(root, "module", "Knowledge System",
                    "The structured, graph-based, searchable knowledge base. "
                    "Claims, provenance, branch-aware views, entity extraction.",
                    order=6)

    kg_corpus = add(knowledge, "component", "Corpus Ingestion",
                    "Ingest conversations, musings, documents into the graph. "
                    "User thoughts as sovereign objects, AI responses as regenerable derivatives.",
                    status="in_progress", order=1)
    add(kg_corpus, "feature", "Conversation Parser", "ChatGPT/Claude markdown → thoughts + derivations", status="in_progress", order=1)
    add(kg_corpus, "feature", "Musing Parser", "Freeform markdown → sectioned thoughts", status="in_progress", order=2)
    add(kg_corpus, "feature", "Entity Extraction", "Named concept recognition from content", status="in_progress", order=3)

    kg_search = add(knowledge, "component", "Search & Query",
                    "Full-text search via FTS5, entity lookup, graph traversal.", order=2)
    add(kg_search, "feature", "Full-Text Search", "FTS5 across thoughts and derivations", status="in_progress", order=1)
    add(kg_search, "feature", "Entity Search", "Find and explore named concepts", status="in_progress", order=2)
    add(kg_search, "feature", "Graph Traversal", "Follow edges, find related content", order=3)

    kg_hier = add(knowledge, "component", "Knowledge Hierarchy",
                  "Tree structure for organizing knowledge. Mission→module→component→feature. "
                  "Enables agent orientation and subtask decomposition.", order=3)

    # ═══════════════════════════════════════════════════════════
    # TIER 3: Agentic Operations
    # ═══════════════════════════════════════════════════════════
    agentic = add(root, "module", "Agentic Operations",
                  "The true core. Agentic loops, mission control, distributed agent coordination. "
                  "Design for capability curve, not current capability point.",
                  order=7)

    ag_harness = add(agentic, "component", "Agent Harness",
                     "Minimal sovereignty substrate for safe agentic loops. "
                     "Intent expression, trust propagation, behavior verification, recovery.", order=1)
    add(ag_harness, "feature", "Intent Expression", "How the operator expresses what they want", order=1)
    add(ag_harness, "feature", "Trust Propagation", "How trust flows through agent chains", order=2)
    add(ag_harness, "feature", "Behavior Verification", "Verify behavior matches contract", order=3)
    add(ag_harness, "feature", "Recovery", "What happens when things go wrong", order=4)

    ag_mission = add(agentic, "component", "Mission Control",
                     "Orchestration of multi-agent systems. Task decomposition, "
                     "assignment, monitoring, coordination.", order=2)

    ag_conv = add(agentic, "component", "Sovereign Conversation",
                  "First Autonomy use case. Your thoughts as persistent sovereign objects, "
                  "AI responses as regenerable derivatives. Replace linear chat transcripts.", order=3)
    add(ag_conv, "feature", "Thought Persistence", "User inputs saved with stable IDs, searchable, combinable", order=1)
    add(ag_conv, "feature", "Context Assembly", "Curate sets of thoughts for projection into new conversations", order=2)
    add(ag_conv, "feature", "Response Regeneration", "AI responses treated as derived, can be recomputed", order=3)
    add(ag_conv, "feature", "Cross-Platform Continuity", "Thoughts from ChatGPT, Claude, local — all unified", order=4)

    # ═══════════════════════════════════════════════════════════
    # TIER 4: Third-Party Integration & Research
    # ═══════════════════════════════════════════════════════════
    thirdparty = add(root, "module", "Third-Party & Research",
                     "Leverage existing work for agentic operations. "
                     "Ralph Loop, Beads, CASS, memory systems, autoresearch patterns.",
                     order=8)

    add(thirdparty, "reference", "Autoresearch / program.md",
        "Karpathy's autoresearch pattern. Natural language specs defining agent behavior. "
        "Distributed coordination via shared memory (Ensue). Emergent agent specialization.", order=1)

    add(thirdparty, "reference", "Ralph Loop",
        "Agent loop pattern. Research and evaluate for integration into mission control.", order=2)

    add(thirdparty, "reference", "Beads",
        "Conversation/context management pattern. Evaluate for sovereign conversation component.", order=3)

    add(thirdparty, "reference", "CASS",
        "Context-aware agent system. Evaluate for harness integration.", order=4)

    add(thirdparty, "reference", "Memory Systems",
        "Persistent agent memory patterns. Evaluate for knowledge system integration.", order=5)

    add(thirdparty, "reference", "CRDTs / Automerge / Loro",
        "Conflict-free replicated data types. Foundation for distributed document/graph replication.", order=6)

    add(thirdparty, "reference", "Peritext",
        "Rich text with overlapping marks. Required for document-layer formatting.", order=7)

    add(thirdparty, "reference", "Pijul Patch Theory",
        "Branch algebra / overlapping patch sets. Study for protocol versioning model.", order=8)

    # ═══════════════════════════════════════════════════════════
    # TIER 5: Governance & Doctrine
    # ═══════════════════════════════════════════════════════════
    governance = add(root, "module", "Governance & Doctrine",
                     "Constitutional principles. Sovereignty line, non-negotiables, "
                     "maturity model, business model.",
                     order=9)

    add(governance, "component", "Sovereignty Doctrine",
        "No shared module may require surrender of local control. "
        "Delegation must never equal surrender. User authority is final.", order=1)

    add(governance, "component", "Non-Negotiables",
        "Users can disable modules, revoke providers, export data. "
        "No hidden server-side policy overrides. Agents stay within permission surface.", order=2)

    add(governance, "component", "Maturity Model",
        "Local → Channel → Experimental → Stable → Core. "
        "Imagination moves fast without destabilizing the network.", order=3)

    add(governance, "component", "Business Model",
        "Monetize service quality, not user captivity. "
        "Excellent infra, surfaces, module implementations, premium data/compute.", order=4)

    return count
