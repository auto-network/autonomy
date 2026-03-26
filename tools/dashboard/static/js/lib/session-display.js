/**
 * Virtual display layer — pure module, no Alpine/DOM dependency.
 *
 * Display descriptors reference store.entries[] by index:
 *   Single:  { idx: N }
 *   Group:   { type: 'group', tool_name: 'Bash', start: N, end: M }
 *
 * Three functions:
 *   buildAll(entries)            — full build (backfill, one time)
 *   appendOne(display, entries)  — incremental O(1) append (live SSE)
 *   resolve(descriptor, entries) — resolve descriptor to entry/group at render time
 */
(function () {
  'use strict';

  var GROUPABLE = { Bash: 1, Read: 1, Edit: 1, Grep: 1, Glob: 1 };

  function isGroupable(entry) {
    return entry.type === 'tool_use' && GROUPABLE[entry.tool_name] === 1;
  }

  /**
   * Build full display array from entries. Groups consecutive same-tool
   * groupable entries (2+) into group descriptors.
   */
  function buildAll(entries) {
    var display = [];
    var i = 0;
    var len = entries.length;
    while (i < len) {
      var e = entries[i];
      if (isGroupable(e)) {
        var j = i + 1;
        while (j < len && entries[j].type === 'tool_use' && entries[j].tool_name === e.tool_name) {
          j++;
        }
        if (j - i >= 2) {
          display.push({ type: 'group', tool_name: e.tool_name, start: i, end: j - 1 });
          i = j;
          continue;
        }
      }
      display.push({ idx: i });
      i++;
    }
    return display;
  }

  /**
   * Append an entry to display. O(1).
   * @param {Array} display - current display descriptors
   * @param {Array} entries - source entries array
   * @param {number} [atIdx] - index of entry to append (default: entries.length - 1)
   * Mutates display in place. Returns display for convenience.
   */
  function appendOne(display, entries, atIdx) {
    var newIdx = (atIdx !== undefined) ? atIdx : entries.length - 1;
    if (newIdx < 0 || newIdx >= entries.length) return display;
    var entry = entries[newIdx];
    var last = display.length > 0 ? display[display.length - 1] : null;

    if (last && isGroupable(entry)) {
      // Case 1: last is a group of the same tool — extend
      if (last.type === 'group' && last.tool_name === entry.tool_name) {
        last.end = newIdx;
        return display;
      }
      // Case 2: last is a single of the same groupable tool — promote to group
      if (last.idx !== undefined) {
        var prevEntry = entries[last.idx];
        if (isGroupable(prevEntry) && prevEntry.tool_name === entry.tool_name) {
          display[display.length - 1] = {
            type: 'group',
            tool_name: entry.tool_name,
            start: last.idx,
            end: newIdx
          };
          return display;
        }
      }
    }

    // Case 3: new single descriptor
    display.push({ idx: newIdx });
    return display;
  }

  /**
   * Resolve a descriptor to the actual entry or group object.
   * Single: returns entries[d.idx] (same reference).
   * Group: returns { type: 'tool_group', tool_name, items, timestamp }.
   */
  function resolve(d, entries) {
    if (d.type === 'group') {
      return {
        type: 'tool_group',
        tool_name: d.tool_name,
        items: entries.slice(d.start, d.end + 1),
        timestamp: entries[d.start] ? entries[d.start].timestamp : undefined
      };
    }
    return entries[d.idx];
  }

  var SessionDisplay = {
    buildAll: buildAll,
    appendOne: appendOne,
    resolve: resolve,
    _isGroupable: isGroupable
  };

  // Dual export: browser (window) and Node (module.exports)
  if (typeof window !== 'undefined') {
    window.SessionDisplay = SessionDisplay;
  }
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = SessionDisplay;
  }
})();
