/**
 * Virtual display layer — pure module, no Alpine/DOM dependency.
 *
 * Display descriptors reference store.entries[] (server truth, sorted by
 * entry_ref tuple) and store.localEntries[] (client-local tiles such as
 * upload attachments, interleaved by timestamp):
 *   Single:  { idx: N,  key }
 *   Local:   { local: N, key }
 *   Group:   { type: 'group', tool_name, start: N, end: M, key }
 *
 * ``key`` is stable across prepends/inserts (derived from entry_ref, not
 * array position) — it keys the x-for DOM identity and all expansion
 * state, so a scroll-up page can never re-parent or collapse tiles the
 * operator already expanded (auto-16g9t: display keyed by entry_ref).
 *
 * Three functions:
 *   buildAll(entries, locals)     — full build (rebuild on any non-tail change)
 *   appendOne(display, entries)   — incremental O(1) tail append (live SSE)
 *   resolve(descriptor, entries, locals) — resolve at render time
 */
(function () {
  'use strict';

  var GROUPABLE = { Bash: 1, exec_command: 1, Read: 1, Edit: 1, Grep: 1, Glob: 1 };

  function isGroupable(entry) {
    return entry.type === 'tool_use' && GROUPABLE[entry.tool_name] === 1;
  }

  function isDisplayable(entry) {
    return entry && entry.internal !== true && entry.hidden !== true;
  }

  function refKey(entry, idx) {
    var r = entry && entry.entry_ref;
    if (r) return r.file + ':' + r.off + ':' + (r.sub || 0);
    return 'i:' + idx;
  }

  function localKey(entry, idx) {
    return 'l:' + (entry.rel_path || '') + ':' + (entry.filename || '') +
      ':' + (entry.timestamp || idx);
  }

  /**
   * Build the full display array: server entries grouped (consecutive
   * same-tool groupable runs of 2+), local tiles interleaved by timestamp.
   */
  function buildAll(entries, locals) {
    locals = locals || [];
    var display = [];
    var li = 0;

    function flushLocalsUpTo(ts) {
      while (li < locals.length &&
             (ts === null || (locals[li].timestamp || '') <= ts)) {
        if (isDisplayable(locals[li])) {
          display.push({ local: li, key: localKey(locals[li], li) });
        }
        li++;
      }
    }

    var i = 0;
    var len = entries.length;
    while (i < len) {
      var e = entries[i];
      if (!isDisplayable(e)) {
        i++;
        continue;
      }
      flushLocalsUpTo(e.timestamp || '');
      if (isGroupable(e)) {
        var j = i + 1;
        while (j < len && isDisplayable(entries[j]) && entries[j].type === 'tool_use' && entries[j].tool_name === e.tool_name) {
          j++;
        }
        if (j - i >= 2) {
          display.push({
            type: 'group', tool_name: e.tool_name, start: i, end: j - 1,
            key: 'g:' + refKey(e, i),
          });
          i = j;
          continue;
        }
      }
      display.push({ idx: i, key: refKey(e, i) });
      i++;
    }
    flushLocalsUpTo(null);
    return display;
  }

  /**
   * Append a tail entry to display. O(1). Only valid for pure tail
   * appends — any mid-buffer insert or local-tile change rebuilds.
   * @param {Array} display - current display descriptors
   * @param {Array} entries - source entries array
   * @param {number} [atIdx] - index of entry to append (default: last)
   * Mutates display in place. Returns display for convenience.
   */
  function appendOne(display, entries, atIdx) {
    var newIdx = (atIdx !== undefined) ? atIdx : entries.length - 1;
    if (newIdx < 0 || newIdx >= entries.length) return display;
    var entry = entries[newIdx];
    if (!isDisplayable(entry)) return display;
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
            end: newIdx,
            key: 'g:' + refKey(prevEntry, last.idx),
          };
          return display;
        }
      }
    }

    // Case 3: new single descriptor
    display.push({ idx: newIdx, key: refKey(entry, newIdx) });
    return display;
  }

  /**
   * Resolve a descriptor to the actual entry or group object.
   * Single: returns entries[d.idx] (same reference).
   * Local:  returns locals[d.local] (same reference).
   * Group:  returns { type: 'tool_group', tool_name, items, timestamp }.
   */
  function resolve(d, entries, locals) {
    if (d.type === 'group') {
      return {
        type: 'tool_group',
        tool_name: d.tool_name,
        items: entries.slice(d.start, d.end + 1),
        timestamp: entries[d.start] ? entries[d.start].timestamp : undefined
      };
    }
    if (d.local !== undefined) {
      return (locals || [])[d.local];
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
