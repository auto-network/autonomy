// Mission plugin — Alpine.data factory referenced by frontend.alpine_root.
// The dashboard-side page is a thin index over /api/mission/missions; the
// mission screens themselves are complete documents served by
// /api/mission/screen/<mission_id>.
window.missionPage = function () {
  return {
    missions: [],
    loaded: false,
    error: "",
    async init() {
      try {
        const r = await fetch("/api/mission/missions");
        if (!r.ok) throw new Error("HTTP " + r.status);
        this.missions = (await r.json()).missions || [];
      } catch (e) {
        this.error = String(e && e.message || e);
      }
      this.loaded = true;
    },
    open(m) {
      window.location.href = "/api/mission/screen/" + m.mission_id;
    },
  };
};
