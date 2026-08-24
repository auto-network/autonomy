// Mission plugin — Alpine.data factory referenced by frontend.alpine_root.
//
// The homepage is activity-first (operator design guidance): full-width
// mission rows, the name on one unwrapped auto-fit line, a 14-day
// activity strip with "last activity" recency, attention counts
// (blockers, in-progress), and an inline lifecycle control. No mission
// descriptions — the screen answers "what moved and what needs me",
// not "what is this mission".
//
// Screens embed in a same-origin iframe with pushState URLs — never a
// page navigation (the shell's dictation layer must survive).
window.missionPage = function () {
  return {
    missions: [],
    loaded: false,
    error: "",
    current: null,

    async init() {
      await this.refresh();
      const m = window.location.pathname.match(/^\/mission\/([0-9a-f-]{8,})/);
      if (m) this.current = m[1];
      window.addEventListener("popstate", () => {
        const mm = window.location.pathname.match(/^\/mission\/([0-9a-f-]{8,})/);
        this.current = mm ? mm[1] : null;
      });
    },

    async refresh() {
      try {
        const r = await fetch("/api/mission/missions");
        if (!r.ok) throw new Error("HTTP " + r.status);
        this.missions = (await r.json()).missions || [];
        this.error = "";
      } catch (e) {
        this.error = String(e && e.message || e);
      }
      this.loaded = true;
    },

    // ── the name: one line, full width, auto-fit between a modest
    //    ceiling and a readable floor (the studio calibration lesson:
    //    never comical, never wrapped) ──
    fitName(el) {
      const fit = () => {
        let size = 1.02;                       // rem ceiling
        el.style.fontSize = size + "rem";
        const avail = () => el.parentElement.clientWidth;
        let guard = 24;
        while (el.scrollWidth > avail() && size > 0.78 && guard--) {
          size -= 0.03;
          el.style.fontSize = size + "rem";
        }
      };
      requestAnimationFrame(fit);
      window.addEventListener("resize", () => requestAnimationFrame(fit));
    },

    ago(m) {
      const at = m.activity && m.activity.last_at;
      if (!at) return "no activity yet";
      const s = Date.now() / 1000 - at;
      if (s < 60) return "active just now";
      if (s < 3600) return "active " + Math.round(s / 60) + "m ago";
      if (s < 172800) return "active " + Math.round(s / 3600) + "h ago";
      return "active " + Math.round(s / 86400) + "d ago";
    },

    bars(m) {
      // The studio activity-grid language: small cells, intensity by
      // count — height AND opacity scale, empty days a faint baseline.
      const days = (m.activity && m.activity.days) || [];
      const max = Math.max(1, ...days);
      return days.map((n) => ({
        on: n > 0,
        h: n ? Math.max(30, Math.round((n / max) * 100)) : 100,
        op: n ? (0.45 + 0.55 * (n / max)).toFixed(2) : 1,
      }));
    },

    counts(m) {
      // The app's icon vocabulary, not words: ⊘ blocked, ⟳ in progress,
      // ? open — numbers beside marks, one line, no wrapping.
      const STOP = '<svg viewBox="0 0 16 16" aria-hidden="true">'
        + '<circle cx="8" cy="8" r="6.2" fill="none" stroke="currentColor"'
        + ' stroke-width="1.7"/><path d="M3.9 3.9L12.1 12.1"'
        + ' stroke="currentColor" stroke-width="1.7"'
        + ' stroke-linecap="round"/></svg>';
      const ARR = '<svg viewBox="0 0 16 16" aria-hidden="true">'
        + '<g transform="rotate(15 8 8)">'
        + '<path d="M13.5 8A5.5 5.5 0 1 1 10.75 3.24" fill="none"'
        + ' stroke="currentColor" stroke-width="1.7"'
        + ' stroke-linecap="round"/>'
        + '<path d="M13.89 5.06L8.21 5.21 11.18 0.05z"'
        + ' fill="currentColor"/></g></svg>';
      const a = m.activity || {};
      const out = [];
      if (a.blockers)
        out.push({key: "b", mark: STOP, n: a.blockers, cls: "crit"});
      if (a.in_progress)
        out.push({key: "p", mark: ARR, n: a.in_progress, cls: "warn"});
      const open = (a.open_questions || 0) - (a.blockers || 0);
      if (open > 0) out.push({key: "q", mark: "?", n: open, cls: "dim"});
      return out;
    },

    lifeOpen: null,
    lifeTap(m, st) {
      if (this.lifeOpen !== m.mission_id) {
        this.lifeOpen = m.mission_id;          // first tap: reveal choices
        return;
      }
      this.lifeOpen = null;
      if (st !== m.status) this.setStatus(m, st);
    },

    async setStatus(m, status) {
      const prev = m.status;
      m.status = status;                        // optimistic
      try {
        const r = await fetch("/api/mission/status/" + m.mission_id, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({status}),
        });
        if (!r.ok) throw new Error("HTTP " + r.status);
      } catch (e) {
        m.status = prev;
      }
    },

    frameSrc() {
      return this.current ? "/api/mission/screen/" + this.current : "";
    },

    open(m) {
      this.current = m.mission_id;
      history.pushState({}, "", "/mission/" + m.mission_id);
    },

    back() {
      this.current = null;
      history.pushState({}, "", "/mission");
      this.refresh();
    },
  };
};
