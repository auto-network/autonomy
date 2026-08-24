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
    loading: false,
    progress: 0,            // 0..100, real signal: stages, bytes, render
    loadNote: "",           // italic sub-status narrated by the server
    win: {key: "30d", secs: 30 * 86400, buckets: 30},
    sparkTick: 0,           // bumped on resize: cards redraw at new width
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
      if (m) { this.current = m[1]; this.loadScreen(m[1]); }
      // The inner screen reports its position (pillar/tab); the URL
      // mirrors it via replaceState — copyable, but NEVER a history
      // entry: the back button stays a pure exit from the plugin.
      window.addEventListener("message", (ev) => {
        const d = ev && ev.data;
        if (!d || d.type !== "mission:where" || !this.current) return;
        const bits = [];
        if (d.view && d.view !== "overview") bits.push("view=" + d.view);
        if (d.section) bits.push("tab=" + d.section);
        history.replaceState(null, "", "/mission/" + this.current
          + (bits.length ? "#" + bits.join("&") : ""));
      });
      document.addEventListener("click", () => { this.popFor = null; });
      let srt = null;
      window.addEventListener("resize", () => {
        clearTimeout(srt);
        srt = setTimeout(() => { this.sparkTick++; }, 180);
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
      try {
        const r2 = await fetch("/api/mission/allocation");
        if (r2.ok) this.alloc = (await r2.json()).missions || [];
      } catch (e) {}
      this.loaded = true;
    },

    currentTitle() {
      if (!this.current) return "";
      const m = this.missions.find((x) => x.mission_id === this.current);
      return (m && m.name) || "";
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

    // Full-bleed card chart: the window's events as one smooth line,
    // drawn imperatively at the card's native pixel width (stretched
    // SVG strokes go jagged) and redrawn when the window or width
    // changes. The overlaid title/pillars sit on the left, so late
    // history — the right side — stays clear.
    drawSpark(el, m, winSecs, _tick) {
      // paint via rAF + ResizeObserver: x-effect can fire before layout
      // settles, and a chart drawn at a stale width letterboxes inside
      // the real card. Measure at paint time, repaint on any resize.
      el.__paint = () => this.paintSpark(el, m, winSecs);
      if (!el.__ro && window.ResizeObserver) {
        el.__ro = new ResizeObserver(() => el.__paint && el.__paint());
        el.__ro.observe(el);
      }
      if (window.requestAnimationFrame)
        requestAnimationFrame(() => el.__paint && el.__paint());
      else el.__paint();
    },

    paintSpark(el, m, winSecs) {
      const rect = el.getBoundingClientRect();
      const W = Math.round(rect.width), H = Math.round(rect.height);
      while (el.firstChild) el.removeChild(el.firstChild);
      if (!W || !H) return;
      el.setAttribute("viewBox", "0 0 " + W + " " + H);
      const cutoff = Date.now() / 1000 - winSecs;
      const N = Math.max(24, Math.min(96, Math.floor(W / 12)));
      // one series per surface, same rendering as inside the mission:
      // the pillar colors ARE the identity, here as at every depth
      const perSid = {};
      ((m.activity && m.activity.events) || []).forEach((ev) => {
        const t = Array.isArray(ev) ? ev[0] : ev;
        const sid = Array.isArray(ev) ? (ev[1] || "") : "";
        const idx = Math.floor((t - cutoff) / (winSecs / N));
        if (idx < 0 || idx >= N) return;
        (perSid[sid] = perSid[sid] || new Array(N).fill(0))[idx]++;
      });
      const K = [1, 3, 6, 8, 6, 3, 1], KS = 28, KH = 3;
      let max = 0.001;
      const series = Object.keys(perSid).map((sid) => {
        const raw = perSid[sid], sm = new Array(N).fill(0);
        let total = 0;
        for (let i = 0; i < N; i++) {
          let acc = 0;
          for (let j = 0; j < K.length; j++) {
            const idx = i + j - KH;
            if (idx >= 0 && idx < N) acc += raw[idx] * K[j];
          }
          sm[i] = acc / KS;
          total += raw[i];
          if (sm[i] > max) max = sm[i];
        }
        return {sid, sm, total};
      });
      series.sort((a, b) => a.total - b.total);   // busiest drawn on top
      const row = this.alloc.find((a) => a.mission_id === m.mission_id);
      const colorOf = (sid) => {
        const pl = ((row && row.pillars) || [])
          .find((p) => p.pillar_id === sid);
        return (pl && pl.color) || "#8b85ff";
      };
      const padT = 6, baseY = H - 1;
      const r = (v) => Math.round(v * 10) / 10;
      const cl = (y) => Math.min(baseY, Math.max(padT, y));
      const NS = "http://www.w3.org/2000/svg";
      series.forEach((sr) => {
        const pts = sr.sm.map((v, i) =>
          [(i + 0.5) / N * W, baseY - (v / max) * (baseY - padT)]);
        let d = "M" + r(pts[0][0]) + "," + r(pts[0][1]);
        for (let i = 0; i < pts.length - 1; i++) {
          const p0 = pts[Math.max(0, i - 1)], p1 = pts[i],
                p2 = pts[i + 1], p3 = pts[Math.min(pts.length - 1, i + 2)];
          d += "C" + r(p1[0] + (p2[0] - p0[0]) / 6) + ","
            + r(cl(p1[1] + (p2[1] - p0[1]) / 6)) + " "
            + r(p2[0] - (p3[0] - p1[0]) / 6) + ","
            + r(cl(p2[1] - (p3[1] - p1[1]) / 6)) + " "
            + r(p2[0]) + "," + r(p2[1]);
        }
        const line = document.createElementNS(NS, "path");
        line.setAttribute("d", d);
        line.setAttribute("fill", "none");
        line.setAttribute("stroke", colorOf(sr.sid));
        line.setAttribute("stroke-width", "1.8");
        line.setAttribute("stroke-opacity", "0.75");
        line.setAttribute("stroke-linecap", "round");
        line.setAttribute("stroke-linejoin", "round");
        el.appendChild(line);
      });
    },

    pillarsOf(m) {
      const row = this.alloc.find((a) => a.mission_id === m.mission_id);
      return ((row && row.pillars) || []).slice(0, 5);
    },

    morePillars(m) {
      const row = this.alloc.find((a) => a.mission_id === m.mission_id);
      return Math.max(0, ((row && row.pillars) || []).length - 5);
    },

    openPillar(m, p) {
      this._focus = p.pillar_id;
      this.current = m.mission_id;
      history.replaceState(null, "", "/mission/" + m.mission_id);
      this.loadScreen(m.mission_id);
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
      history.replaceState(null, "", "/mission/" + m.mission_id);
      this.loadScreen(m.mission_id);
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
      history.replaceState(null, "", "/mission/" + m.mission_id);
      this.loadScreen(m.mission_id);
    },

    // The interstitial is honest twice over: the server streams stage
    // markers (<!--msn:pct|note-->) while it composes — including the
    // "N of M settings" count — so the bar and sub-status narrate the
    // real work, then document bytes carry it to 95% and the frame's
    // own load event closes it.
    async loadScreen(id) {
      this.loading = true;
      this.progress = 2;
      this.loadNote = "";
      const base = "/api/mission/screen/" + id;
      const src = base + "?progress=1"
        + (this._focus ? "&pillar=" + encodeURIComponent(this._focus) : "");
      const strip = (text) => {
        // markers precede the doctype; srcdoc gets the clean document
        const i = text.indexOf("<!doctype");
        return i > 0 ? text.slice(i) : text;
      };
      const mount = (html) => {
        this.$nextTick(() => {
          const f = document.getElementById("msn-frame");
          if (!f) { this.loading = false; return; }
          f.onload = () => {
            this.progress = 100;
            setTimeout(() => { this.loading = false; }, 120);
          };
          if (html != null) f.srcdoc = strip(html);
          else f.src = base + (this._focus
            ? "?pillar=" + encodeURIComponent(this._focus) : "");
          this.progress = Math.max(this.progress, 95);
        });
      };
      try {
        const r = await fetch(src);
        if (!r.ok) throw new Error("HTTP " + r.status);
        if (r.body && r.body.getReader) {
          const reader = r.body.getReader();
          const dec = new TextDecoder();
          const stageRe = /<!--msn:(\d+)\|([^>]*?)-->/g;
          let text = "", docAt = -1, docBytes = 0;
          for (;;) {
            const {done, value} = await reader.read();
            if (done) break;
            text += dec.decode(value, {stream: true});
            let m, last = null;
            stageRe.lastIndex = 0;
            while ((m = stageRe.exec(text))) last = m;
            if (last) {
              this.progress = Math.max(this.progress,
                Math.min(88, parseInt(last[1], 10) || 0));
              this.loadNote = last[2];
            }
            if (docAt < 0) {
              const dm = text.match(/<!--msn:doc:(\d+)-->/);
              if (dm) {
                docAt = text.indexOf(dm[0]) + dm[0].length;
                docBytes = parseInt(dm[1], 10) || 0;
                this.loadNote = "Receiving document";
              }
            }
            if (docAt >= 0 && docBytes) {
              const ratio = Math.min(1, (text.length - docAt) / docBytes);
              this.progress = Math.max(this.progress,
                88 + Math.round(ratio * 7));
            }
          }
          text += dec.decode();
          mount(docAt >= 0 ? text.slice(docAt) : text);
        } else {
          mount(await r.text());
        }
      } catch (e) {
        mount(null);                           // let the iframe try itself
      }
    },

    back() {
      this.current = null;
      this._focus = null;
      history.replaceState(null, "", "/mission");
      this.refresh();
    },
  };
};
