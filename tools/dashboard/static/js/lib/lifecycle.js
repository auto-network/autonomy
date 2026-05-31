// auto-yfcoc: derivation helpers for the session-card startup phase UI.
//
// Pure functions (no DOM, no state) — operate on a session row ``s`` of
// the shape the partial already consumes (``s.is_live``, ``s.setup_phase``,
// ``s.harness_phase``, ``s.resumable``, ``s.harness_state``, ``s.entry_count``,
// ``s.last_activity``, etc.) and return the data-shape the design's
// chip/lifecycle/action markup expects.
//
// Design rev 63542418-40f2-40cd-972b-7ae1a50f41a3 (Session Card Lifecycle
// States) maps 13 derivable states from the (setup_phase, harness_phase)
// pair plus ``is_live`` + ``resumable``. The derivation here is
// data-driven over that pair — intermediate states (entrypoint_running,
// dind_ready, setup_running) that the backend doesn't emit live yet
// will light up automatically when their markers land later.
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
      if (setupPhase === "dind_ready") return "dind_ready";
      if (setupPhase === "entrypoint_running") return "entrypoint";
      if (setupPhase === "container_starting") return "container_starting";
    }
    if (harnessPhase === "first_turn_written") return "first_turn_written";
    if (harnessPhase === "harness_starting") return "harness_starting";

    return "pending";
  }

  // ── Phase chip label + tone (per the design's 14 fixture states) ──
  var _STATE_CHIP_LABEL = {
    pending: "Starting…",
    container_starting: "Starting container",
    entrypoint: "Running entrypoint",
    dind_ready: "Docker ready",
    setup_running: "Installing dependencies",
    harness_starting: "Booting harness",
    first_turn_written: "Almost ready",
    ready: "",
    ready_with_confirming_trust: "Confirming trust…",
    ready_with_planning: "Planning",
    setup_failed: "Setup failed",
    dead_resumable: "",
    dead_not_resumable: "",
  };

  // Tone modifier class for .sc-phase-chip. "" (default) = sky-blue
  // active startup pulse; "ready" = static green; "failed" = static
  // amber; "dead" = static slate. Returned as a className string so
  // the template can drop it directly into :class.
  var _STATE_CHIP_TONE = {
    ready: "ready",
    ready_with_confirming_trust: "",      // amber-like overlay; keep active pulse for visibility
    ready_with_planning: "ready",          // teal-via-design uses ready tone with planning label
    setup_failed: "failed",
    dead_resumable: "dead",
    dead_not_resumable: "dead",
  };

  function phaseChip(s) {
    return _STATE_CHIP_LABEL[lifecycleState(s)] || "";
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
      var ORDER = ["pending", "container_starting", "entrypoint_running", "dind_ready", "setup_running", "setup_complete"];
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

  // The 4-lane caption row beneath the track. Each lane is a
  // ``{label, value}`` pair the template renders verbatim.
  function lifecycleLanes(s) {
    var state = lifecycleState(s);
    var setupPhase = (s && s.setup_phase) || "pending";
    var harnessPhase = (s && s.harness_phase) || "pending";

    function setupLabel() {
      if (setupPhase === "setup_failed") return "failed";
      if (setupPhase === "setup_complete") return "complete";
      if (setupPhase === "setup_running") return "running";
      if (setupPhase === "dind_ready") return "dind ready";
      if (setupPhase === "entrypoint_running") return "entrypoint";
      if (setupPhase === "container_starting") return "starting";
      return "—";
    }
    function harnessLabel() {
      if (harnessPhase === "composer_ready") return "ready";
      if (harnessPhase === "first_turn_written") return "1st turn";
      if (harnessPhase === "harness_starting") return "booting";
      return "—";
    }

    return [
      { label: "container", value: state === "pending" ? "queued" : "up" },
      { label: "setup", value: setupLabel() },
      { label: "harness", value: harnessLabel() },
      { label: "ready", value: state === "ready" || state === "ready_with_confirming_trust" || state === "ready_with_planning" ? "yes" : "no" },
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
    };
  }
})();
