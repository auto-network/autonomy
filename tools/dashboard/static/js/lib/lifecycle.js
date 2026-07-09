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
  // ── Derivation: server state → UI state ──
  //
  // The backend sends the one lifecycle truth on every row: ``state`` ∈
  // LAUNCHING | ACTIVE | STOPPING | ENDED | FAILED (also mirrored as
  // ``lifecycle_state``). The client maps it to a UI state key and adds
  // ONLY presentation refinements: the granular chip phase while
  // LAUNCHING/STOPPING (``startup_state`` — legit sub-state, not a second
  // truth), resumability for ENDED, and the planning-mode overlay for
  // ACTIVE. The legacy-field fallback below covers rows that predate the
  // state field (old fixtures, cached payloads) and nothing else — the
  // client no longer re-derives lifecycle from is_live/activity_state
  // pairs when the server said what the state is.
  function lifecycleState(s) {
    if (!s) return "pending";
    var st = s.state || s.lifecycle_state;
    if (st === "FAILED") return "setup_failed";
    if (st === "ENDED") {
      return s.resumable ? "dead_resumable" : "dead_not_resumable";
    }
    if (st === "LAUNCHING" || st === "STOPPING") {
      return s.startup_state || (st === "STOPPING" ? "stopping" : "requesting");
    }
    if (st === "ACTIVE") {
      var hsA = s.harness_state || {};
      if (hsA.in_planning_mode) return "ready_with_planning";
      return "ready";
    }
    // ── Legacy fallback (no state field on the row) ──
    if (s.activity_state === "failed" || s.startup_state === "setup_failed") {
      return "setup_failed";
    }
    // Truthy test rather than ``=== false``: the dead path delivers
    // is_live as the integer 0 (SQLite) or missing.
    if (!s.is_live) {
      if (s.resumable) return "dead_resumable";
      return "dead_not_resumable";
    }
    if (s.startup_state) {
      return s.startup_state;
    }
    var hs = s.harness_state || {};
    if (hs.in_planning_mode) return "ready_with_planning";
    return "ready";
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
  //   2. ``harness_starting`` renders the static "Starting harness"
  //      label for every harness. (The earlier dynamic
  //      "Booting <Harness>" form was dropped in the unified-FSM
  //      rework, auto-ja51w C10.)
  var _STATE_CHIP_LABEL = {
    pending: "",
    requesting: "Queued",
    preparing_workspace: "Preparing workspace",  // augmented in phaseChip() with N/M
    launching_container: "Starting container",
    container_starting: "Starting container",
    entrypoint_running: "Initializing container",
    setup_running: "Container setup",
    harness_starting: "Starting harness",
    confirming_trust: "Confirming trust",
    composer_ready: "Sending orientation",
    awaiting_first_response: "Awaiting first reply",
    stopping: "Stopping",
    cleaning: "Stopping",
    setup_failed: "Setup failed",
    ready: "",
    ready_with_planning: "Planning",
    dead_resumable: "Ended",
    dead_not_resumable: "Ended",
  };

  // Tone modifier class for .sc-phase-chip. "" (default) = sky-blue
  // active startup pulse; "ready" = static green; "failed" = static
  // amber; "dead" = static slate.
  var _STATE_CHIP_TONE = {
    ready: "ready",
    ready_with_planning: "",
    setup_failed: "failed",
    confirming_trust: "failed",  // amber attention state — operator may need to intervene
    dead_resumable: "dead",
    dead_not_resumable: "dead",
  };

  function phaseChip(s) {
    var state = lifecycleState(s);
    if (state === "preparing_workspace") {
      // Augment with the N/M repo progress from the SSE registry payload's
      // phase_progress dict (set by SessionMonitor.update_phase from inside
      // prepare_session_mounts' per-repo callback).
      var p = s && s.phase_progress;
      if (p && typeof p.repo_index === "number" && typeof p.total === "number") {
        return "Preparing workspace " + p.repo_index + "/" + p.total;
      }
    }
    return _STATE_CHIP_LABEL[state] || "";
  }

  function phaseTone(s) {
    return _STATE_CHIP_TONE[lifecycleState(s)] || "";
  }

  // ── Lifecycle strip — visible during launching states ──
  //
  // Returns true when the session is in any launching state (startup_state
  // IS NOT NULL on the backend → lifecycleState returns the state key
  // directly). False for ready, dead, and the planning-mode overlay.
  function startupVisible(s) {
    var state = lifecycleState(s);
    if (state === "ready") return false;
    if (state === "ready_with_planning") return false;
    if (state === "dead_resumable") return false;
    if (state === "dead_not_resumable") return false;
    return true;
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
      inlineAction: inlineAction,
      messageTone: messageTone,
      // [lc] instrumentation — callers own the memo + emit decision.
      emit: _emit,
      summarize: summarize,
    };
  }
})();
