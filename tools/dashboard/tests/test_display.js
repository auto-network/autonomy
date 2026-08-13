/**
 * Node.js unit tests for session-display.js — virtual display layer.
 * Run: node --test tools/dashboard/tests/test_display.js
 *
 * Pins the CURRENT descriptor contract (auto-16g9t keys, auto-64nx3
 * anchor rule + immutable ref-keyed group membership):
 *   Single: { idx: N, key }   Local: { local: N, key }
 *   Group:  { type: 'group', tool_name, refs: [...], key: 'g:' + refs[0] }
 * Keys derive from entry_ref (file:off:sub) with 'i:'+idx fallback.
 * Rendering starts at the first ANCHOR; entries above it are held.
 */
const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const { buildAll, appendOne, resolve, isAnchor, _isGroupable } =
  require('../static/js/lib/session-display.js');

// ── Helpers ──────────────────────────────────────────────────────────

function tool(name, id) {
  return { type: 'tool_use', tool_name: name, tool_id: id || name.toLowerCase(), timestamp: '2026-01-01T00:00:00Z' };
}
function user(text) {
  return { type: 'user', content: text, timestamp: '2026-01-01T00:00:00Z' };
}
function asst(text) {
  return { type: 'assistant_text', content: text, timestamp: '2026-01-01T00:00:00Z' };
}
function thinking(text) {
  return { type: 'thinking', content: text, timestamp: '2026-01-01T00:00:00Z' };
}
/** byRef map the way the store builds it: refKey(entry, idx) -> entry. */
function byRefOf(entries) {
  const map = {};
  entries.forEach((e, i) => {
    const r = e && e.entry_ref;
    map[r ? `${r.file}:${r.off}:${r.sub || 0}` : `i:${i}`] = e;
  });
  return map;
}

// ── Suite 1: buildAll ────────────────────────────────────────────────

describe('buildAll', () => {
  it('empty entries', () => {
    assert.deepStrictEqual(buildAll([]), []);
  });

  it('single anchor entry', () => {
    const d = buildAll([user('hello')]);
    assert.deepStrictEqual(d, [{ idx: 0, key: 'i:0' }]);
  });

  it('anchor rule: a tool-only buffer renders nothing', () => {
    // No anchor above them, so grouped tools are HELD, not rendered.
    const d = buildAll([tool('Bash', 'b1'), tool('Bash', 'b2')]);
    assert.deepStrictEqual(d, []);
  });

  it('anchor rule: entries above the first anchor are held', () => {
    const entries = [tool('Bash', 'b1'), user('hi'), tool('Read', 'r1')];
    const d = buildAll(entries);
    assert.deepStrictEqual(d, [
      { idx: 1, key: 'i:1' },
      { idx: 2, key: 'i:2' },
    ]);
  });

  it('anchor rule: an empty user carrier is not an anchor', () => {
    const d = buildAll([user(''), asst('flow starts here')]);
    assert.deepStrictEqual(d, [{ idx: 1, key: 'i:1' }]);
  });

  it('consecutive same-tool groupable entries form a ref-keyed group', () => {
    const entries = [user('go'), tool('Bash', 'b1'), tool('Bash', 'b2'), tool('Bash', 'b3')];
    const d = buildAll(entries);
    assert.equal(d.length, 2);
    assert.deepStrictEqual(d[1], {
      type: 'group', tool_name: 'Bash',
      refs: ['i:1', 'i:2', 'i:3'], key: 'g:i:1',
    });
  });

  it('mixed entries: user + tools + assistant', () => {
    const entries = [user('hi'), tool('Read', 'r1'), tool('Read', 'r2'), asst('done')];
    const d = buildAll(entries);
    assert.deepStrictEqual(d, [
      { idx: 0, key: 'i:0' },
      { type: 'group', tool_name: 'Read', refs: ['i:1', 'i:2'], key: 'g:i:1' },
      { idx: 3, key: 'i:3' },
    ]);
  });

  it('non-groupable tool_use stays as single', () => {
    const entries = [asst('spawning'), tool('Agent', 'a1'), tool('Agent', 'a2')];
    const d = buildAll(entries);
    assert.deepStrictEqual(d, [
      { idx: 0, key: 'i:0' },
      { idx: 1, key: 'i:1' },
      { idx: 2, key: 'i:2' },
    ]);
  });

  it('text entry between same tools breaks the group', () => {
    const entries = [asst('start'), tool('Edit', 'e1'), asst('note'), tool('Edit', 'e2')];
    const d = buildAll(entries);
    assert.equal(d.length, 4);
    assert.deepStrictEqual(d.map((x) => x.idx), [0, 1, 2, 3]);
  });

  it('single groupable tool_use not grouped (needs 2+)', () => {
    const entries = [user('run'), tool('Bash', 'b1'), asst('ok')];
    const d = buildAll(entries);
    assert.deepStrictEqual(d.map((x) => x.idx), [0, 1, 2]);
  });

  it('full conversation pattern', () => {
    const entries = [
      user('fix it'),           // 0
      thinking('let me think'), // 1
      tool('Read', 'r1'),      // 2
      tool('Read', 'r2'),      // 3
      tool('Edit', 'e1'),      // 4
      tool('Edit', 'e2'),      // 5
      tool('Edit', 'e3'),      // 6
      asst('done'),            // 7
      tool('Bash', 'b1'),      // 8
    ];
    const d = buildAll(entries);
    assert.equal(d.length, 6);
    assert.deepStrictEqual(d[0], { idx: 0, key: 'i:0' });
    assert.deepStrictEqual(d[1], { idx: 1, key: 'i:1' });
    assert.deepStrictEqual(d[2], { type: 'group', tool_name: 'Read', refs: ['i:2', 'i:3'], key: 'g:i:2' });
    assert.deepStrictEqual(d[3], { type: 'group', tool_name: 'Edit', refs: ['i:4', 'i:5', 'i:6'], key: 'g:i:4' });
    assert.deepStrictEqual(d[4], { idx: 7, key: 'i:7' });
    assert.deepStrictEqual(d[5], { idx: 8, key: 'i:8' });
  });

  it('all groupable tools are recognized', () => {
    const tools = ['Bash', 'exec_command', 'Read', 'Edit', 'Grep', 'Glob'];
    for (const t of tools) {
      const entries = [user('go'), tool(t, t + '1'), tool(t, t + '2')];
      const d = buildAll(entries);
      assert.equal(d.length, 2, t + ' should be grouped');
      assert.equal(d[1].type, 'group');
      assert.equal(d[1].tool_name, t);
    }
  });

  it('internal entries are not displayed', () => {
    const entries = [
      user('hi'),
      { type: 'codex_task_complete', role: 'system', internal: true },
      asst('done'),
    ];
    const d = buildAll(entries);
    assert.deepStrictEqual(d, [{ idx: 0, key: 'i:0' }, { idx: 2, key: 'i:2' }]);
  });

  it('keys derive from entry_ref when present (stable across shifts)', () => {
    const entries = [
      { ...user('hi'), entry_ref: { file: 'a.jsonl', off: 100 } },
      { ...tool('Bash', 'b1'), entry_ref: { file: 'a.jsonl', off: 220, sub: 2 } },
      { ...tool('Bash', 'b2'), entry_ref: { file: 'a.jsonl', off: 300 } },
    ];
    const d = buildAll(entries);
    assert.deepStrictEqual(d, [
      { idx: 0, key: 'a.jsonl:100:0' },
      {
        type: 'group', tool_name: 'Bash',
        refs: ['a.jsonl:220:2', 'a.jsonl:300:0'], key: 'g:a.jsonl:220:2',
      },
    ]);
  });
});

// ── Suite 2: appendOne ───────────────────────────────────────────────

describe('appendOne', () => {
  it('append anchor to empty display', () => {
    const entries = [user('hi')];
    const d = [];
    appendOne(d, entries);
    assert.deepStrictEqual(d, [{ idx: 0, key: 'i:0' }]);
  });

  it('anchor rule: a non-anchor tail onto an empty display is held', () => {
    const entries = [tool('Bash', 'b1')];
    const d = [];
    appendOne(d, entries);
    assert.deepStrictEqual(d, []);
  });

  it('append non-groupable entry', () => {
    const entries = [user('hi'), asst('hello')];
    const d = [{ idx: 0, key: 'i:0' }];
    appendOne(d, entries);
    assert.deepStrictEqual(d, [{ idx: 0, key: 'i:0' }, { idx: 1, key: 'i:1' }]);
  });

  it('extend existing group membership with same tool', () => {
    const entries = [tool('Bash', 'b1'), tool('Bash', 'b2'), tool('Bash', 'b3')];
    const d = [{ type: 'group', tool_name: 'Bash', refs: ['i:0', 'i:1'], key: 'g:i:0' }];
    appendOne(d, entries);
    assert.equal(d.length, 1);
    assert.deepStrictEqual(d[0].refs, ['i:0', 'i:1', 'i:2']);
    assert.equal(d[0].key, 'g:i:0', 'group key is stable while membership grows');
  });

  it('promote single to group when same groupable tool appended', () => {
    const entries = [tool('Read', 'r1'), tool('Read', 'r2')];
    const d = [{ idx: 0, key: 'i:0' }];
    appendOne(d, entries);
    assert.deepStrictEqual(d, [
      { type: 'group', tool_name: 'Read', refs: ['i:0', 'i:1'], key: 'g:i:0' },
    ]);
  });

  it('different tool breaks into new single', () => {
    const entries = [tool('Bash', 'b1'), tool('Read', 'r1')];
    const d = [{ idx: 0, key: 'i:0' }];
    appendOne(d, entries);
    assert.equal(d.length, 2);
    assert.deepStrictEqual(d[1], { idx: 1, key: 'i:1' });
  });

  it('non-groupable tool_use not promoted', () => {
    const entries = [tool('Agent', 'a1'), tool('Agent', 'a2')];
    const d = [{ idx: 0, key: 'i:0' }];
    appendOne(d, entries);
    assert.equal(d.length, 2);
    assert.deepStrictEqual(d[1], { idx: 1, key: 'i:1' });
  });

  it('mutates display in place', () => {
    const entries = [user('x'), user('y')];
    const d = [{ idx: 0, key: 'i:0' }];
    const result = appendOne(d, entries);
    assert.strictEqual(result, d);
    assert.equal(d.length, 2);
  });

  it('skips internal entries incrementally', () => {
    const entries = [user('x'), { type: 'codex_task_complete', internal: true }];
    const d = [{ idx: 0, key: 'i:0' }];
    appendOne(d, entries);
    assert.deepStrictEqual(d, [{ idx: 0, key: 'i:0' }]);
  });

  it('incremental appendOne matches buildAll', () => {
    const entries = [
      user('go'),
      tool('Bash', 'b1'), tool('Bash', 'b2'),
      asst('ok'),
      tool('Edit', 'e1'), tool('Edit', 'e2'), tool('Edit', 'e3'),
      user('done'),
    ];
    const incremental = [];
    for (let i = 0; i < entries.length; i++) {
      appendOne(incremental, entries.slice(0, i + 1));
    }
    assert.deepStrictEqual(incremental, buildAll(entries));
  });
});

// ── Suite 3: resolve ─────────────────────────────────────────────────

describe('resolve', () => {
  it('single descriptor resolves to entry reference (fast path)', () => {
    const entries = [user('hi'), asst('hello')];
    const resolved = resolve({ idx: 0, key: 'i:0' }, entries);
    assert.strictEqual(resolved, entries[0]);
  });

  it('single with shifted index resolves BY REF, never wrong', () => {
    const e = { ...user('hi'), entry_ref: { file: 'a.jsonl', off: 100 } };
    const entries = [asst('merged-in above'), e]; // e moved from idx 0 to 1
    const stale = { idx: 0, key: 'a.jsonl:100:0' }; // built before the merge
    const resolved = resolve(stale, entries, [], byRefOf(entries));
    assert.strictEqual(resolved, e, 'ref identity finds the moved entry');
  });

  it('single whose entry vanished paints STALE, not a wrong entry', () => {
    const entries = [asst('someone else')];
    const resolved = resolve({ idx: 0, key: 'i:9' }, entries, [], {});
    assert.equal(resolved.type, '__stale__');
    assert.equal(resolved.hidden, true);
  });

  it('group descriptor resolves membership by ref to tool_group', () => {
    const entries = [tool('Bash', 'b1'), tool('Bash', 'b2'), tool('Bash', 'b3')];
    const d = { type: 'group', tool_name: 'Bash', refs: ['i:0', 'i:1', 'i:2'], key: 'g:i:0' };
    const resolved = resolve(d, entries, [], byRefOf(entries));
    assert.equal(resolved.type, 'tool_group');
    assert.equal(resolved.tool_name, 'Bash');
    assert.equal(resolved.items.length, 3);
    assert.strictEqual(resolved.items[0], entries[0]);
    assert.strictEqual(resolved.items[2], entries[2]);
    assert.equal(resolved.timestamp, entries[0].timestamp);
  });

  it('group member that vanished is omitted (missing, not wrong)', () => {
    const entries = [tool('Read', 'r1'), tool('Read', 'r2')];
    const byRef = byRefOf(entries);
    const d = { type: 'group', tool_name: 'Read', refs: ['i:0', 'i:9', 'i:1'], key: 'g:i:0' };
    const resolved = resolve(d, entries, [], byRef);
    assert.equal(resolved.items.length, 2);
  });

  it('group with no resolvable members is STALE', () => {
    const d = { type: 'group', tool_name: 'Read', refs: ['i:5'], key: 'g:i:5' };
    assert.equal(resolve(d, [], [], {}).type, '__stale__');
  });

  it('group without a byRef map is STALE (never interval-resolved)', () => {
    const entries = [tool('Bash', 'b1'), tool('Bash', 'b2')];
    const d = { type: 'group', tool_name: 'Bash', refs: ['i:0', 'i:1'], key: 'g:i:0' };
    assert.equal(resolve(d, entries).type, '__stale__');
  });
});

// ── Suite 4: entry mutation ──────────────────────────────────────────

describe('entry mutation', () => {
  it('mutation on source entry is visible through descriptor', () => {
    const entries = [user('original')];
    const d = buildAll(entries);
    entries[0].content = 'rewritten';
    entries[0].rewritten = true;
    const resolved = resolve(d[0], entries);
    assert.equal(resolved.content, 'rewritten');
    assert.equal(resolved.rewritten, true);
  });

  it('display array unchanged after source mutation', () => {
    const entries = [user('go'), tool('Bash', 'b1'), tool('Bash', 'b2')];
    const d = buildAll(entries);
    const before = JSON.stringify(d);
    entries[1].input = { command: 'ls -la' };
    entries[2].input = { command: 'pwd' };
    assert.equal(JSON.stringify(d), before,
      'display descriptors should not change when entries are mutated');
  });
});

// ── Suite 5: isAnchor (the operator-specified rule, pinned) ──────────

describe('isAnchor', () => {
  it('a user message with text anchors', () => {
    assert.equal(isAnchor(user('hello')), true);
  });
  it('a bare tool-result carrier does not anchor', () => {
    assert.equal(isAnchor(user('')), false);
    assert.equal(isAnchor(user('   ')), false);
    assert.equal(isAnchor({ type: 'user' }), false);
  });
  it('agent flow anchors: assistant text and thinking', () => {
    assert.equal(isAnchor(asst('done')), true);
    assert.equal(isAnchor(thinking('hmm')), true);
  });
  it('a peer crosstalk message anchors', () => {
    assert.equal(isAnchor({ type: 'crosstalk', content: 'ping' }), true);
  });
  it('tool use never anchors', () => {
    assert.equal(isAnchor(tool('Bash', 'b1')), false);
  });
  it('groupable classifier still exported for the store', () => {
    assert.equal(_isGroupable(tool('Bash', 'b1')), true);
    assert.equal(_isGroupable(tool('Agent', 'a1')), false);
  });
});
