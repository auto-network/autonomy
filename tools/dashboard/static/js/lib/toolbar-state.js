/**
 * Toolbar state machine — pure module, no Alpine/DOM dependency.
 *
 * Dual-export matching session-display.js pattern:
 *   Node.js: module.exports = { deriveToolbarState, toolbarElements }
 *   Browser: window.deriveToolbarState, window.toolbarElements
 */
(function () {
  'use strict';

  function deriveToolbarState(chatOpen, chatConnected) {
    if (chatOpen && chatConnected) return 'LIVE_CHAT';
    if (chatOpen) return 'PICKER';
    if (chatConnected) return 'LIVE_UI';
    return 'DISCONNECTED';
  }

  function toolbarElements(state) {
    return {
      state: state,
      showIterNav: state === 'DISCONNECTED' || state === 'LIVE_UI',
      captureVisible: state === 'LIVE_UI',
      row2Session: state === 'LIVE_CHAT',
      titleText: state === 'PICKER' ? null : 'experiment',
      primeVisible: state === 'LIVE_CHAT',
      disconnectVisible: state === 'LIVE_CHAT',
      chatIconClass: {
        DISCONNECTED: 'chat-disconnected',
        PICKER: 'chat-open',
        LIVE_UI: 'chat-connected-hidden',
        LIVE_CHAT: 'chat-connected-shown',
      }[state],
    };
  }

  // Dual export
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { deriveToolbarState: deriveToolbarState, toolbarElements: toolbarElements };
  } else {
    window.deriveToolbarState = deriveToolbarState;
    window.toolbarElements = toolbarElements;
  }
})();
