"""Dynamic primer builder for the mission curator librarian.

Called by the dispatcher at launch time. The payload IS the run's scope —
the template in prompt.md carries the protocol; everything target-specific
(which mission, which surfaces, what qualifies as a candidate, how many
writes, dry run or live, who gets the report) arrives here from whoever
enqueued the job.

Payload keys:
    mission_id       — required; the mission to curate. Refused if unknown,
                       so a typo fails the job loudly instead of launching
                       an agent aimed at nothing.
    pillars          — optional list of pillar names/ids; default = the
                       overview and every pillar (resolved here, at build
                       time, so the agent receives an explicit enumeration
                       rather than a phrase like "the mission" — the
                       pass-1 lesson: scope by inventory, never by noun).
    kinds            — optional list of item kinds eligible for judgment
                       (default: decision, question, work, incident).
    min_body_chars   — bodies at/over this length in a terminal state are
                       candidates (default 1200).
    write_budget     — max write actions (default 6).
    dry_run          — when true, the agent reports exact commands instead
                       of running them.
    report_to        — session name to CrossTalk the report to (required).
"""

from __future__ import annotations

DEFAULT_KINDS = ["decision", "question", "work", "incident"]


def build_primer(payload: dict) -> str:
    mission_id = (payload.get("mission_id") or "").strip()
    report_to = (payload.get("report_to") or "").strip()
    if not mission_id:
        raise ValueError("mission_curate payload requires mission_id")
    if not report_to:
        raise ValueError("mission_curate payload requires report_to")

    from tools.dashboard.dao import mission_control_db as db

    mission = db.get_mission(mission_id)
    if not mission:
        raise ValueError(f"mission_curate: unknown mission {mission_id!r}")
    all_pillars = db.list_pillars(mission_id)

    wanted = payload.get("pillars")
    if wanted:
        wanted_l = [str(w).lower() for w in wanted]
        pillars = [p for p in all_pillars
                   if p["pillar_id"] in wanted
                   or any(w in p["name"].lower() for w in wanted_l)]
        if not pillars:
            raise ValueError(
                f"mission_curate: pillars {wanted!r} match nothing on "
                f"{mission['name']!r}")
    else:
        pillars = all_pillars

    kinds = payload.get("kinds") or DEFAULT_KINDS
    min_body = int(payload.get("min_body_chars") or 1200)
    budget = int(payload.get("write_budget") or 6)
    dry = bool(payload.get("dry_run"))

    surfaces = "\n".join(
        [f"- Overview surface: `{mission_id}` (the mission itself)"]
        + [f"- Pillar: {p['name']} — `{p['pillar_id']}`" for p in pillars]
    )
    mode = (
        "MODE: DRY RUN. Execute ZERO write commands. Report the exact "
        "command you would have run for every non-KEEP verdict."
        if dry else
        f"MODE: LIVE. Write budget: at most {budget} write actions."
    )
    return f"""# Run scope — mission curation pass

Mission: "{mission['name']}" (`{mission_id}`), org {mission.get('org') or '?'}.
This is your ONLY target. Never touch any other mission or any setting
outside this mission's items.

Surfaces to enumerate — ALL of them:
{surfaces}

Candidate rules (deterministic — apply before judging anything):
- kind in {{{', '.join(kinds)}}} where fields substantially repeat one
  another (fork vs title vs chosen), or a field opens as a dialogue turn
  ("No.", "Yes,").
- any listed kind with body length >= {min_body} characters AND state in
  {{proven, done, settled}}.
- Never candidates: checkpoints, scope, status, metrics, and anything in
  state open/active/blocked/next — live work is not yours.

{mode}

Report to session `{report_to}` via CrossTalk before stopping."""
