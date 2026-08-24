// Mission plugin — Alpine.data factory referenced by frontend.alpine_root.
//
// Ported verbatim from the Design Studio iteration (design ff8b7ff9):
// activity-first rows, lifecycle legend + timeframe zoom on one line,
// tappable counts with titled preview bubbles, title-as-navigation,
// lifecycle popover with a double-tap guard. The org selector belongs
// to the shell's nav bar (not this page) and arrives with that
// substrate slot.
//
// Screens embed in a same-origin iframe with pushState URLs — never a
// page navigation (the shell's dictation layer must survive).
window.missionPage = function () {
  return {
    missions: [],
    loaded: false,
    error: "",
    current: null,
    lifeFilter: {},
    lifeOpen: null,
    lifeArmedAt: 0,
    popFor: null,           // "<mission_id>:<countKey>" with an open bubble
    win: {key: "30d", secs: 30 * 86400, buckets: 30},
    screen: "missions",       // missions | sessions
    alloc: [],
    orgs: [],
    org: localStorage.getItem("msn.org") || "",
    orgOpen: false,
    windows: [
      {key: "24h", secs: 86400, buckets: 24},
      {key: "7d", secs: 7 * 86400, buckets: 28},
      {key: "30d", secs: 30 * 86400, buckets: 30},
    ],

    async init() {
      // Claim the shell toolbar: the org selector teleports into
      // #app-topbar-slot; the SPA router's resetTopbar removes the
      // class on navigation away.
      const header = document.querySelector("header");
      if (header) header.classList.add("app-topbar-active");
      try {
        const d = await fetch("/api/orgs").then((r) => r.ok ? r.json() : {});
        this.orgs = (d.orgs || []).map((e) => {
          const b = (e && e.org) || {};
          const idp = (e && e.identity && e.identity.payload) || {};
          return {slug: b.slug || idp.slug || "",
                  name: idp.name || b.slug || "?",
                  color: idp.color || "#64748b",
                  initial: idp.initial || (idp.name || "?")[0]};
        }).filter((o) => o.slug && o.slug !== "personal");
      } catch (e) {}
      await this.refresh();
      // Default org: where the missions actually are — never a blind
      // first-of-list. A stored choice with zero missions is stale
      // (org renamed, missions moved): fall back the same way.
      const orgsWithMissions = this.missions.map((m) => m.org);
      if (!this.org || !orgsWithMissions.includes(this.org)) {
        this.org = orgsWithMissions[0] || this.org
          || (this.orgs[0] || {}).slug || "";
      }
      const m = window.location.pathname.match(/^\/mission\/([0-9a-f-]{8,})/);
      if (m) this.current = m[1];
      window.addEventListener("popstate", () => {
        const mm = window.location.pathname.match(/^\/mission\/([0-9a-f-]{8,})/);
        this.current = mm ? mm[1] : null;
      });
      document.addEventListener("click", () => { this.popFor = null; });
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
      try {
        const r2 = await fetch("/api/mission/allocation");
        if (r2.ok) this.alloc = (await r2.json()).missions || [];
      } catch (e) {}
      this.loaded = true;
    },

    curOrg() {
      return this.orgs.find((o) => o.slug === this.org) || null;
    },

    setOrg(slug) {
      this.org = slug;
      this.orgOpen = false;
      localStorage.setItem("msn.org", slug);
    },

    allocVisible() {
      return this.alloc.filter((m) => !this.org || m.org === this.org);
    },

    // ── window filter: only missions live inside the frame ──
    visible() {
      const cutoff = Date.now() / 1000 - this.win.secs;
      const act = Object.keys(this.lifeFilter)
        .filter((k) => this.lifeFilter[k]);
      return this.missions.filter((m) => {
        if (this.org && m.org && m.org !== this.org) return false;
        const at = m.activity && m.activity.last_at;
        if (!at || at < cutoff) return false;
        if (act.length && !act.includes(m.status || "active")) return false;
        return true;
      });
    },

    legend() {
      // Same universe as visible(): org filter + window — the counts
      // and the list must never disagree.
      const cutoff = Date.now() / 1000 - this.win.secs;
      const inWin = this.missions.filter(
        (m) => (!this.org || !m.org || m.org === this.org)
               && (m.activity && m.activity.last_at || 0) >= cutoff);
      const n = (st) => inWin.filter(
        (m) => (m.status || "active") === st).length;
      return [
        {word: "active", n: n("active"), color: "var(--accent)"},
        {word: "paused", n: n("paused"), color: "var(--warn)"},
        {word: "complete", n: n("complete"), color: "var(--good)"},
      ];
    },

    anyFilter() {
      return Object.keys(this.lifeFilter).some((k) => this.lifeFilter[k]);
    },

    // The shell's content area has no definite height, so flex chains
    // collapse to zero for absolutely-sized children like iframes.
    // Measure instead: the frame gets exactly the viewport below it.
    fitFrame(el) {
      const fit = () => {
        const top = el.getBoundingClientRect().top;
        el.style.height = Math.max(240, window.innerHeight - top) + "px";
      };
      requestAnimationFrame(fit);
      window.addEventListener("resize", () => requestAnimationFrame(fit));
      if (window.visualViewport)
        window.visualViewport.addEventListener(
          "resize", () => requestAnimationFrame(fit));
    },

    fitName(el) {
      const fit = () => {
        let size = 1.02;
        el.style.fontSize = size + "rem";
        let guard = 24;
        while (el.scrollWidth > el.parentElement.clientWidth
               && size > 0.78 && guard--) {
          size -= 0.03;
          el.style.fontSize = size + "rem";
        }
      };
      requestAnimationFrame(fit);
      window.addEventListener("resize", () => requestAnimationFrame(fit));
    },

    rel(at) {
      const s = Date.now() / 1000 - at;
      if (s < 60) return "just now";
      if (s < 3600) return Math.round(s / 60) + "m ago";
      if (s < 172800) return Math.round(s / 3600) + "h ago";
      return Math.round(s / 86400) + "d ago";
    },

    statusLine(m) {
      const at = m.activity && m.activity.last_at;
      if ((m.status || "active") === "active")
        return at ? "active " + this.rel(at) : "no activity yet";
      const parts = [m.status + (m.status_changed_at
        ? " " + this.rel(Date.parse(m.status_changed_at) / 1000) : "")];
      if (at) parts.push("last activity " + this.rel(at));
      return parts.join(" · ");
    },

    bars(m) {
      const cutoff = Date.now() / 1000 - this.win.secs;
      const nb = this.win.buckets;
      const span = this.win.secs / nb;
      const buckets = new Array(nb).fill(0);
      ((m.activity && m.activity.events) || []).forEach((e) => {
        const idx = Math.floor((e - cutoff) / span);
        if (idx >= 0 && idx < nb) buckets[idx]++;
      });
      const max = Math.max(1, ...buckets);
      return buckets.map((n) => ({
        on: n > 0,
        h: n ? Math.max(30, Math.round((n / max) * 100)) : 100,
        op: n ? (0.45 + 0.55 * (n / max)).toFixed(2) : 1,
      }));
    },

    counts(m) {
      if ((m.status || "active") !== "active") return [];
      const a = m.activity || {};
      const pv = a.previews || {};
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
      // The bubble explains the number in one line and links in — no
      // content previews (operator ruling: long strings overflowed).
      const out = [];
      if (a.blockers) out.push({key: "b", mark: STOP, n: a.blockers,
        cls: "crit",
        desc: "<b>" + a.blockers + "</b> open question"
              + (a.blockers > 1 ? "s are" : " is")
              + " blocking progress"});
      if (a.in_progress) out.push({key: "p", mark: ARR, n: a.in_progress,
        cls: "warn",
        desc: "<b>" + a.in_progress + "</b> acceptance criteri"
              + (a.in_progress > 1 ? "a are" : "on is")
              + " being worked"});
      const open = (a.open_questions || 0) - (a.blockers || 0);
      if (open > 0) out.push({key: "q", mark: "?", n: open, cls: "dim",
        desc: "<b>" + open + "</b> question"
              + (open > 1 ? "s await" : " awaits") + " an answer"});
      return out;
    },

    togglePop(m, c) {
      const id = m.mission_id + ":" + c.key;
      this.popFor = this.popFor === id ? null : id;
    },

    openAt(m) {
      this.popFor = null;
      this._focus = null;
      this.current = m.mission_id;
      history.pushState({}, "", "/mission/" + m.mission_id);
    },

    lifeTapCur(m) {
      this.lifeOpen = this.lifeOpen === m.mission_id ? null : m.mission_id;
      this.lifeArmedAt = Date.now() + 350;
    },

    async lifePick(m, st) {
      if (Date.now() < this.lifeArmedAt) return;   // stray double-tap
      this.lifeOpen = null;
      if (st === m.status) return;
      const prev = m.status;
      m.status = st;
      try {
        const r = await fetch("/api/mission/status/" + m.mission_id, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({status: st}),
        });
        if (!r.ok) throw new Error("HTTP " + r.status);
        m.status_changed_at = new Date().toISOString();
      } catch (e) {
        m.status = prev;
      }
    },

    frameSrc() {
      if (!this.current) return "";
      const q = this._focus
        ? "?pillar=" + encodeURIComponent(this._focus) : "";
      return "/api/mission/screen/" + this.current + q;
    },

    open(m) {
      this._focus = null;
      this.current = m.mission_id;
      history.pushState({}, "", "/mission/" + m.mission_id);
    },

    back() {
      this.current = null;
      this._focus = null;
      history.pushState({}, "", "/mission");
      this.refresh();
    },
  };
};
