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

  // ── The anchor rule (auto-64nx3, operator-specified) ────────────────
  //
  // An ANCHOR is an entry whose meaning needs nothing before it: a
  // user's actual text message (a bare tool-result carrier is NOT one),
  // or the start of agent flow (assistant text / thinking). The display
  // renders the contiguous buffer from the FIRST anchor downward;
  // anything above the topmost anchor is HELD — kept in the buffer,
  // never rendered, and no fetch is made on its behalf. When scroll-up
  // merges the next older page, the buffer becomes contiguous through
  // the held entries and they render exactly once under their owning
  // flow; an owner that never arrives in view means they never render.
  //
  // This is a render-time if-statement, nothing more. It deletes the
  // dangling-fragment class by construction: a tool-result carrier can
  // never render as an empty USER tile and tool output can never render
  // as ASSISTANT prose, because nothing renders without its owning flow
  // present (IMG_2110, live on a phone over an agent transcript).
  function isAnchor(entry) {
    if (!entry) return false;
    if (entry.type === 'user') {
      var c = entry.content;
      if (typeof c === 'string') return c.trim().length > 0;
      return c !== undefined && c !== null;
    }
    // A peer message is a user message with a different label.
    if (entry.type === 'crosstalk') return true;
    // Agent flow: everything below is attributed to this agent's turn.
    return entry.type === 'assistant_text' || entry.type === 'thinking';
  }

  function firstAnchorIndex(entries) {
    for (var i = 0; i < entries.length; i++) {
      if (isAnchor(entries[i])) return i;
    }
    return -1;
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

    // Anchor rule: render from the first anchor downward; entries above
    // it are held. No anchor in the buffer → nothing renders yet.
    var anchorAt = firstAnchorIndex(entries);
    if (anchorAt === -1) {
      flushLocalsUpTo(null);
      return display;
    }
    var i = anchorAt;
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
          // Membership is IMMUTABLE ref keys captured at build time —
          // never a numeric interval (round-3 review: an interval
          // re-read at paint time admits same-tool entries from a
          // DIFFERENT flow that shift into the range).
          var refs = [];
          for (var g = i; g < j; g++) refs.push(refKey(entries[g], g));
          display.push({
            type: 'group', tool_name: e.tool_name, refs: refs,
            key: 'g:' + refs[0],
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
    // Anchor rule: a tail entry renders only at-or-below an anchor. A
    // server descriptor already in the display proves an anchor above;
    // otherwise this entry must itself be one (local upload tiles don't
    // anchor server flow).
    var hasServerDescriptor = false;
    for (var di = display.length - 1; di >= 0; di--) {
      if (display[di].idx !== undefined || display[di].type === 'group') {
        hasServerDescriptor = true;
        break;
      }
    }
    if (!hasServerDescriptor && !isAnchor(entry)) return display;
    var last = display.length > 0 ? display[display.length - 1] : null;

    if (last && isGroupable(entry)) {
      // Case 1: last is a group of the same tool — extend membership
      if (last.type === 'group' && last.tool_name === entry.tool_name) {
        last.refs.push(refKey(entry, newIdx));
        return display;
      }
      // Case 2: last is a single of the same groupable tool — promote to group
      if (last.idx !== undefined) {
        var prevEntry = entries[last.idx];
        if (isGroupable(prevEntry) && prevEntry.tool_name === entry.tool_name) {
          var pk = refKey(prevEntry, last.idx);
          display[display.length - 1] = {
            type: 'group',
            tool_name: entry.tool_name,
            refs: [pk, refKey(entry, newIdx)],
            key: 'g:' + pk,
          };
          return display;
        }
      }
    }

    // Case 3: new single descriptor
    display.push({ idx: newIdx, key: refKey(entry, newIdx) });
    return display;
  }

  // Paint-safe placeholder for a descriptor whose target shifted out
  // from under it (a merge landed between descriptor build and this
  // paint). Every template branch skips it — the transient failure mode
  // is a MISSING tile for one frame, never a WRONG one. The rebuild
  // that follows the merge replaces the descriptor set.
  var STALE = Object.freeze({ type: '__stale__', internal: true, hidden: true });

  /**
   * Resolve a descriptor to the actual entry or group object.
   *
   * Identity-checked (the transient-desync fix): descriptors carry ref
   * keys; resolution goes through identity, never through a live index
   * or numeric interval. A stale descriptor therefore resolves to the
   * CORRECT entry wherever it moved, or paints MISSING (the STALE
   * sentinel / an omitted member) — never a wrong entry.
   *
   * Complexity, honestly stated: singles are O(1) per call (one refKey
   * compare on the fast path, one byRef map lookup on the fallback).
   * Groups are O(m) per call over their m member refs, and the group
   * template calls resolveEntry several times per tile per paint, so a
   * group tile's paint cost is O(m · calls) — small in practice (m is a
   * consecutive same-tool run, calls ≈ 10) and NOT memoized here:
   * descriptors live inside Alpine-reactive arrays, and caching onto
   * them from inside a render effect risks re-render feedback.
   *
   * Single: returns the entry (same reference).
   * Local:  returns locals[d.local] (key-checked).
   * Group:  returns { type: 'tool_group', tool_name, items, timestamp }
   *         — items resolved per-member BY REF from the immutable
   *         membership captured at build time; unresolved members are
   *         omitted; an empty resolution is STALE.
   */
  function resolve(d, entries, locals, byRef) {
    if (d.type === 'group') {
      if (!byRef || !Array.isArray(d.refs)) return STALE;
      var items = [];
      for (var i = 0; i < d.refs.length; i++) {
        var member = byRef[d.refs[i]];
        // Identity resolution: the ref names exactly one logical entry;
        // a member that vanished is OMITTED (missing-not-wrong). The
        // tool guard is belt-and-suspenders — an entry's identity never
        // changes type, so this only trips on a corrupted map.
        if (member && member.type === 'tool_use' && member.tool_name === d.tool_name) {
          items.push(member);
        }
      }
      if (!items.length) return STALE;
      return {
        type: 'tool_group',
        tool_name: d.tool_name,
        items: items,
        timestamp: items[0].timestamp
      };
    }
    if (d.local !== undefined) {
      var loc = (locals || [])[d.local];
      if (!loc || localKey(loc, d.local) !== d.key) return STALE;
      return loc;
    }
    var e = entries[d.idx];
    if (e && refKey(e, d.idx) === d.key) return e;
    e = byRef ? byRef[d.key] : undefined;
    return e || STALE;
  }

  var SessionDisplay = {
    buildAll: buildAll,
    appendOne: appendOne,
    resolve: resolve,
    isAnchor: isAnchor,
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
