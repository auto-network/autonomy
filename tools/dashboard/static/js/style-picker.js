// Style Picker — universal swipeable control (substrate.C).
//
// A generic "pick one of N" affordance: a pill of dots that spreads out
// on tap/drag and collapses back down when idle. It knows nothing about
// any particular plugin or what the options mean — it takes a list of
// {id, color, icon} and fires a bubbling `picker-change` CustomEvent
// ({id, index} in .detail) on selection. Any ancestor listens with
// @picker-change; there is no callback prop and no host-specific code
// inside this file.
//
//   function myPage() {
//     return {
//       ...
//       onPickerChange(e) { this.viewStyle = e.detail.id; },
//     };
//   }
//
//   <div x-data="myPage()" @picker-change="onPickerChange($event)">
//     <div x-data="StylePicker.alpine(MY_ICONS)">
//       <div class="picker" x-ref="picker" :class="expanded ? 'expanded' : 'idle'"
//            :style="'width:' + pickerWidth + 'px'" @pointerdown="onDown($event)">
//         <template x-for="(s, i) in icons" :key="s.id">...</template>
//       </div>
//     </div>
//   </div>
//
// First shipped inside Mission Control's presence redesign, but the
// contract above is the whole surface — nothing here assumes missions,
// revisions, or presence. Ported first for Mission Control's home page,
// meant for reuse anywhere a page wants a user-driven view-style switch.
// Signpost: graph://dff97eec-c59.
(function () {
  'use strict';

  function stylePicker(icons) {
    return {
      icons: icons,
      active: 0,
      expanded: false,
      dragging: false,
      collapseTimer: null,

      IDLE_GAP: 7,       // px between dot centers, clustered -- tight
      SPREAD_GAP: 46,    // px between dot centers, expanded -- finger-friendly

      get pickerWidth() {
        var n = this.icons.length;
        var gap = this.expanded ? this.SPREAD_GAP : this.IDLE_GAP;
        var pad = this.expanded ? 44 : 30;
        return (n - 1) * gap + pad;
      },
      dotOffset(i) {
        var n = this.icons.length;
        var gap = this.expanded ? this.SPREAD_GAP : this.IDLE_GAP;
        return (i - (n - 1) / 2) * gap;
      },

      onDown(e) {
        clearTimeout(this.collapseTimer);
        var wasExpanded = this.expanded;
        this.expanded = true;
        this.dragging = true;
        // The tap that OPENS the picker is just that -- an open. The dots
        // are clustered too tightly to aim at while closed, so selecting
        // from that touch point would pick whatever happened to be under
        // your finger, not what you meant. Only a tap on an ALREADY-open
        // (spread out, aimable) picker selects immediately; otherwise
        // selection starts on the next real movement.
        if (wasExpanded) this._select(e);
        window.addEventListener('pointermove', this._move);
        window.addEventListener('pointerup', this._up);
      },
      _move: null,
      _up: null,

      init() {
        this._move = (e) => { if (this.dragging) this._select(e); };
        this._up = () => {
          this.dragging = false;
          window.removeEventListener('pointermove', this._move);
          window.removeEventListener('pointerup', this._up);
          this.collapseTimer = setTimeout(() => { this.expanded = false; }, 700);
        };
      },

      destroy() {
        clearTimeout(this.collapseTimer);
        window.removeEventListener('pointermove', this._move);
        window.removeEventListener('pointerup', this._up);
      },

      _select(e) {
        var rect = this.$refs.picker.getBoundingClientRect();
        var centerX = rect.left + rect.width / 2;
        var dx = e.clientX - centerX;
        var n = this.icons.length;
        var raw = dx / this.SPREAD_GAP + (n - 1) / 2;
        var next = Math.max(0, Math.min(n - 1, Math.round(raw)));
        if (next !== this.active) {
          this.active = next;
          this.$dispatch('picker-change', { id: this.icons[next].id, index: next });
        }
      },
    };
  }

  var StylePickerNS = { alpine: stylePicker };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = StylePickerNS;
  } else if (typeof window !== 'undefined') {
    window.StylePicker = StylePickerNS;
  }
})();
