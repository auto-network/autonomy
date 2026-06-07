// auto-yfcoc: derivation helpers for the session-card startup phase UI.
//
// Pure functions (no DOM, no state) — operate on a session row ``s`` of
// the shape the partial already consumes (``s.is_live``, ``s.setup_phase``,
// ``s.harness_phase``, ``s.resumable``, ``s.harness_state``, ``s.entry_count``,
// ``s.last_activity``, etc.) and return the data-shape the design's
// chip/lifecycle/action markup expects.
//
// Design rev 63542418-40f2-40cd-972b-7ae1a50f41a3 (Session Card Lifecycle
// States) maps derivable states from the (setup_phase, harness_phase)
// pair plus ``is_live`` + ``resumable``. The derivation here is
// data-driven over that pair — intermediate states (entrypoint_running,
// setup_running) light up automatically as their markers are written.
//
// Exposed via ``window.Autonomy.lifecycle.*`` — same global-namespace
// pattern the voice helpers (``window.Autonomy.voice.ui.*``) use, so
// the shared session-card partial can call them without dragging a
// scope dependency through every page Alpine factory.

(function () {
  // ── Derivation: the (setup_phase, harness_phase) pair → UI state ──
  //
  // Returns one of the canonical state keys the design fixtures name.
  // ``ready`` is the derived AND of (setup_complete, composer_ready).
  // Dead sessions branch off ``is_live`` before the pair is consulted.
  function lifecycleState(s) {
    if (!s) return "pending";
    if (s.is_live === false) {
      if (s.resumable) return "dead_resumable";
      return "dead_not_resumable";
    }
    var setupPhase = s.setup_phase || "pending";
    var harnessPhase = s.harness_phase || "pending";

    if (setupPhase === "setup_failed") return "setup_failed";

    // STARTUP-OVER GUARDS (post-launch fixes). The setup_phase column
    // is unreliable for two real reasons, both surfaced by live-data
    // verification against the real registry:
    //
    //   1. Pre-existing sessions that were running BEFORE the schema
    //      migration ran have setup_phase + harness_phase defaulted
    //      to 'pending' by the column defaults — they never went
    //      through the api_session_create path. (25 of 27 live rows
    //      in the first merge attempt.)
    //
    //   2. The setup-exit watcher isn't re-armed on dashboard restart,
    //      so an idle session that booted before the last restart can
    //      have setup_phase stuck at 'container_starting' forever even
    //      though the harness is fully ready. (1 of 27 in the same
    //      verification — auto-0531-152256: idle 77min at composer_ready
    //      but setup_phase frozen at container_starting because nobody
    //      ever sent a first prompt + the watcher didn't pick up the
    //      old .setup-exit file after the dashboard restart.)
    //
    // Two independent signals authoritatively say "startup is over":
    //
    //   • ``resolved === true`` — the harness has produced JSONL output
    //     (covers case 1 — long-running sessions have data on disk).
    //
    //   • ``harness_phase === "composer_ready"`` — the harness screen-
    //     reader has explicitly observed the composer prompt (covers
    //     case 2 — idle sessions that never typed anything but ARE
    //     past their boot screen).
    //
    // Either-or bypasses to ready. setup_failed still wins because the
    // failed check above this block fires first — "setup script failed"
    // is a state the operator MUST see regardless of whether the
    // harness happens to be at composer_ready.
    if (s.resolved === true || harnessPhase === "composer_ready") {
      var hsResolved = s.harness_state || {};
      if (hsResolved.confirming_trust_prompt) return "ready_with_confirming_trust";
      if (hsResolved.in_planning_mode) return "ready_with_planning";
      return "ready";
    }

    // Ready = setup_complete + composer_ready. Within ready, harness_state
    // flags expose the in-flight overlay (confirming trust, planning).
    if (
      setupPhase === "setup_complete" &&
      harnessPhase === "composer_ready"
    ) {
      var hs = s.harness_state || {};
      if (hs.confirming_trust_prompt) return "ready_with_confirming_trust";
      if (hs.in_planning_mode) return "ready_with_planning";
      return "ready";
    }

    // The two phases run in parallel (dind-entrypoint.sh backgrounds
    // /startup.sh AND exec's the harness concurrently), so when both
    // are mid-progress we prefer the SETUP side — operators reason
    // about "container coming up" first, then "harness booting" once
    // setup is complete. After setup_complete, the harness progression
    // becomes the visible chip.
    if (setupPhase !== "setup_complete") {
      if (setupPhase === "setup_running") return "setup_running";
      // dind_ready dropped — verified by host-0531-020038 audit (turn 549):
      // dind-entrypoint.sh writes only entrypoint_running + setup_running.
      // The dind_ready marker is never written by any production code path.
      if (setupPhase === "entrypoint_running") return "entrypoint";
      if (setupPhase === "container_starting") return "container_starting";
    }
    if (harnessPhase === "first_turn_written") return "first_turn_written";
    if (harnessPhase === "harness_starting") return "harness_starting";

    return "pending";
  }

  // ── Phase chip label + tone (per design rev 63542418 fixtures) ──
  //
  // Strings come straight from the design's fixture payload. The L1
  // test pins them so accidental drift surfaces in CI. Two deliberate
  // deviations from the fixture, both flagged here for the next
  // reader:
  //
  //   1. ``ready`` returns "" (suppress) rather than the design's
  //      "Ready" green pill. A persistent badge on every healthy
  //      session is visual noise; the green is signal-by-absence.
  //      Approved as an explicit deviation by host-0531-020038
  //      (turn 71) — not a design match.
  //   2. ``harness_starting`` label is "Booting " + capitalized
  //      ``s.harness`` (or "harness" fallback). The design hardcodes
  //      "Booting Claude" which mis-labels Codex sessions; the
  //      dynamic form is host-approved (turn 71).
  var _STATE_CHIP_LABEL = {
    pending: "Queued",
    container_starting: "Starting container",
    entrypoint: "Preparing workspace",
    setup_running: "Setup running",
    // harness_starting handled in phaseChip() — dynamic over s.harness
    first_turn_written: "Verifying input",
    ready: "",                              // deliberate deviation, see comment above
    ready_with_confirming_trust: "Confirming trust",
    ready_with_planning: "Planning",
    setup_failed: "Setup failed",
    dead_resumable: "Ended",
    dead_not_resumable: "Ended",
  };

  // Tone modifier class for .sc-phase-chip. "" (default) = sky-blue
  // active startup pulse; "ready" = static green; "failed" = static
  // amber; "dead" = static slate. Returned as a className string the
  // template drops directly into :class.
  var _STATE_CHIP_TONE = {
    ready: "ready",                         // moot — chip is suppressed
    ready_with_confirming_trust: "failed",  // amber attention state per design
    ready_with_planning: "",                // sky-blue default per design
    setup_failed: "failed",
    dead_resumable: "dead",
    dead_not_resumable: "dead",
  };

  function phaseChip(s) {
    var state = lifecycleState(s);
    if (state === "harness_starting") {
      // Dynamic over s.harness so Codex sessions don't mis-render as
      // "Booting Claude" (the design's hardcoded text). Capitalize
      // the first letter for readability.
      var h = (s && s.harness) || "harness";
      var label = h.charAt(0).toUpperCase() + h.slice(1);
      return "Booting " + label;
    }
    return _STATE_CHIP_LABEL[state] || "";
  }

  function phaseTone(s) {
    return _STATE_CHIP_TONE[lifecycleState(s)] || "";
  }

  // ── Lifecycle strip — visible during startup + on terminal failure ──
  //
  // Returns false for ``ready`` and the two dead states (the chrome
  // collapses back to normal density there) and true for every active
  // startup phase + setup_failed (so operators see WHERE it failed).
  function startupVisible(s) {
    var state = lifecycleState(s);
    if (state === "ready") return false;
    if (state === "ready_with_confirming_trust") return false;
    if (state === "ready_with_planning") return false;
    if (state === "dead_resumable") return false;
    if (state === "dead_not_resumable") return false;
    return true;
  }

  // The 4-segment track: container / setup / harness / first turn. Each
  // segment is one of ``done`` / ``active`` / ``failed`` / "" (pending).
  function lifecycleStages(s) {
    var setupPhase = (s && s.setup_phase) || "pending";
    var harnessPhase = (s && s.harness_phase) || "pending";
    var failed = setupPhase === "setup_failed";

    function setupState(target) {
      // ``target`` is the segment's expected setup_phase. "done" when
      // the actual phase has progressed past the target; "active" when
      // the actual phase is at the target; "failed" propagates only to
      // the segment that owns the in-flight failure.
      // dind_ready dropped — never written by dind-entrypoint.sh in production
      // (host-0531-020038 audit turn 549). Phase order is the real progression.
      var ORDER = ["pending", "container_starting", "entrypoint_running", "setup_running", "setup_complete"];
      var actual = ORDER.indexOf(setupPhase);
      var want = ORDER.indexOf(target);
      if (actual < 0 || want < 0) return "";
      if (failed && want === ORDER.length - 1) return "failed";
      if (actual > want) return "done";
      if (actual === want) return "active";
      return "";
    }

    function harnessState(target) {
      var ORDER = ["pending", "harness_starting", "first_turn_written", "composer_ready"];
      var actual = ORDER.indexOf(harnessPhase);
      var want = ORDER.indexOf(target);
      if (actual < 0 || want < 0) return "";
      if (actual > want) return "done";
      if (actual === want) return "active";
      return "";
    }

    return [
      { key: "container", state: setupState("container_starting") },
      { key: "setup", state: failed ? "failed" : setupState("setup_running") },
      { key: "harness", state: harnessState("harness_starting") },
      { key: "first_turn", state: harnessState("first_turn_written") },
    ];
  }

  // The 4-lane caption row beneath the track. Lane labels are fixed
  // per the design — request / setup / harness / input — and the
  // values come straight from the fixture per state. Table-driven so
  // the design's exact text appears verbatim on each card; falls
  // through to a sensible default for unmapped (live but pre-broadcast)
  // states so a stale store row never renders blanks.
  var _STATE_LANE_VALUES = {
    pending:             ["allocating", "waiting",    "pending", "queued"],
    container_starting:  ["created",    "container",  "pending", "queued"],
    entrypoint:          ["created",    "entrypoint", "pending", "queued"],
    setup_running:       ["created",    "running",    "booting", "queued"],
    harness_starting:    ["created",    "complete",   "booting", "queued"],
    first_turn_written:  ["created",    "complete",   "jsonl",   "checking"],
    setup_failed:        ["created",    "failed",     "blocked", "held"],
  };

  function lifecycleLanes(s) {
    var state = lifecycleState(s);
    var values = _STATE_LANE_VALUES[state] || ["—", "—", "—", "—"];
    return [
      { label: "request", value: values[0] },
      { label: "setup",   value: values[1] },
      { label: "harness", value: values[2] },
      { label: "input",   value: values[3] },
    ];
  }

  // ── Inline action affordance for the card footer ─────────────────
  //
  // ``setup_failed`` → Retry. Dead+resumable → Resume. Live → nothing
  // (the existing actions menu owns Stop). Returns null when no
  // inline action applies.
  function inlineAction(s) {
    var state = lifecycleState(s);
    if (state === "setup_failed") return { kind: "retry", label: "Retry" };
    if (state === "dead_resumable") return { kind: "resume", label: "Resume" };
    return null;
  }

  // Message-line tone helper. The template applies the returned class
  // additively to .sc-message; empty string means "no tone change".
  function messageTone(s) {
    var state = lifecycleState(s);
    if (state === "setup_failed") return "sc-failed-message";
    if (startupVisible(s)) return "sc-startup-message";
    return "";
  }

  // ── [lc] timeline instrumentation ────────────────────────────────
  //
  // Single-arg JSON log line that the host's capture parser reads as
  // `line[5:]` after matching the ``[lc] `` prefix. Transitions only —
  // callers compute the (sid, surface) memo and decide when to emit so
  // we never spam per-frame. Format coordinated with host-0531-020038
  // turn 227.
  function _emit(rec) {
    try {
      if (typeof console === "undefined" || !console.log) return;
      var out = {
        t: (typeof performance !== "undefined" && performance.now) ? performance.now() : null,
        wallt: Date.now(),
      };
      for (var k in rec) {
        if (Object.prototype.hasOwnProperty.call(rec, k)) out[k] = rec[k];
      }
      console.log("[lc]", JSON.stringify(out));
    } catch (_) { /* best-effort */ }
  }

  // Snapshot of every lifecycle-derived property a caller needs to
  // compare against the previous frame to decide whether anything
  // material changed. Stable shape — append-only.
  function summarize(s) {
    return {
      state: lifecycleState(s),
      chip: phaseChip(s),
      tone: phaseTone(s),
      visible: startupVisible(s),
      launching: !!(s && s._launching),
      setup_phase: (s && s.setup_phase) || null,
      harness_phase: (s && s.harness_phase) || null,
      resolved: !!(s && s.resolved === true),
    };
  }

  // Expose the namespace. window.Autonomy may already be defined by
  // the voice helpers — coexist by extending it, never replacing.
  if (typeof window !== "undefined") {
    window.Autonomy = window.Autonomy || {};
    window.Autonomy.lifecycle = {
      lifecycleState: lifecycleState,
      phaseChip: phaseChip,
      phaseTone: phaseTone,
      startupVisible: startupVisible,
      lifecycleStages: lifecycleStages,
      lifecycleLanes: lifecycleLanes,
      inlineAction: inlineAction,
      messageTone: messageTone,
      // [lc] instrumentation — callers own the memo + emit decision.
      emit: _emit,
      summarize: summarize,
    };
  }
})();
