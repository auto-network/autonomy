// Mission plugin — Alpine.data factory referenced by frontend.alpine_root.
//
// The mission screens are complete documents served by
// /api/mission/screen/<id>, but they must render INSIDE the dashboard
// SPA: a window.location navigation would tear down the shell — and the
// voice/dictation layer lives in the shell, so it died on every mission
// open (found by the operator, dictation forced off). The list embeds
// the document in a same-origin iframe and manages the URL with
// pushState, so opening a mission and swiping back never leave the SPA.
//
// Dictation seam through the frame: the chat composer registers
// window.__missionChatSend / postMessage("mission:chat-send") inside the
// document; the voice layer reaches it via frame.contentWindow.
window.missionPage = function () {
  return {
    missions: [],
    loaded: false,
    error: "",
    current: null,          // mission_id rendered in the frame, or null

    async init() {
      try {
        const r = await fetch("/api/mission/missions");
        if (!r.ok) throw new Error("HTTP " + r.status);
        this.missions = (await r.json()).missions || [];
      } catch (e) {
        this.error = String(e && e.message || e);
      }
      this.loaded = true;
      // Deep link: /mission/<mission_id> opens that mission in place.
      const m = window.location.pathname.match(/^\/mission\/([0-9a-f-]{8,})/);
      if (m) this.current = m[1];
      window.addEventListener("popstate", () => {
        const mm = window.location.pathname.match(/^\/mission\/([0-9a-f-]{8,})/);
        this.current = mm ? mm[1] : null;
      });
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
    },
  };
};
