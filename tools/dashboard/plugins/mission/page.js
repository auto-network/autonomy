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

    // ── the name: one line, full width, auto-fit (measure, never wrap;
    //    shrink only as far as needed, floor keeps it readable) ──
    fitName(el) {
      const fit = () => {
        let size = 1.35;                       // rem ceiling — never comical
        el.style.fontSize = size + "rem";
        el.style.whiteSpace = "nowrap";
        const avail = () => el.parentElement.clientWidth;
        let guard = 24;
        while (el.scrollWidth > avail() && size > 0.82 && guard--) {
          size -= 0.04;
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
      const days = (m.activity && m.activity.days) || [];
      const max = Math.max(1, ...days);
      return days.map((n) => ({
        h: n ? Math.max(18, Math.round((n / max) * 100)) : 6,
        on: n > 0,
      }));
    },

    counts(m) {
      const a = m.activity || {};
      const out = [];
      if (a.blockers) out.push({label: a.blockers + " blocked", cls: "crit"});
      if (a.in_progress) out.push({label: a.in_progress + " in progress",
                                   cls: "warn"});
      if (a.open_questions && a.open_questions > (a.blockers || 0))
        out.push({label: (a.open_questions - (a.blockers || 0)) + " open",
                  cls: "dim"});
      return out;
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
