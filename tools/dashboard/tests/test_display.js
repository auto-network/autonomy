/**
 * Node.js unit tests for session-display.js — virtual display layer.
 * Run: node --test tools/dashboard/tests/test_display.js
 */
const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const { buildAll, appendOne, resolve, _isGroupable } = require('../static/js/lib/session-display.js');

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

// ── Suite 1: buildAll ────────────────────────────────────────────────

describe('buildAll', () => {
  it('empty entries', () => {
    const d = buildAll([]);
    assert.deepStrictEqual(d, []);
  });

  it('single entry', () => {
    const entries = [user('hello')];
    const d = buildAll(entries);
    assert.deepStrictEqual(d, [{ idx: 0 }]);
  });

  it('consecutive same-tool groupable entries form a group', () => {
    const entries = [tool('Bash', 'b1'), tool('Bash', 'b2'), tool('Bash', 'b3')];
    const d = buildAll(entries);
    assert.equal(d.length, 1);
    assert.deepStrictEqual(d[0], { type: 'group', tool_name: 'Bash', start: 0, end: 2 });
  });

  it('mixed entries: user + tools + assistant', () => {
    const entries = [user('hi'), tool('Read', 'r1'), tool('Read', 'r2'), asst('done')];
    const d = buildAll(entries);
    assert.equal(d.length, 3);
    assert.deepStrictEqual(d[0], { idx: 0 });
    assert.deepStrictEqual(d[1], { type: 'group', tool_name: 'Read', start: 1, end: 2 });
    assert.deepStrictEqual(d[2], { idx: 3 });
  });

  it('non-groupable tool_use stays as single', () => {
    const entries = [tool('Agent', 'a1'), tool('Agent', 'a2')];
    const d = buildAll(entries);
    assert.equal(d.length, 2);
    assert.deepStrictEqual(d[0], { idx: 0 });
    assert.deepStrictEqual(d[1], { idx: 1 });
  });

  it('text entry between same tools breaks the group', () => {
    const entries = [tool('Edit', 'e1'), asst('note'), tool('Edit', 'e2')];
    const d = buildAll(entries);
    assert.equal(d.length, 3);
    assert.deepStrictEqual(d[0], { idx: 0 });
    assert.deepStrictEqual(d[1], { idx: 1 });
    assert.deepStrictEqual(d[2], { idx: 2 });
  });

  it('single groupable tool_use not grouped (needs 2+)', () => {
    const entries = [tool('Bash', 'b1'), asst('ok')];
    const d = buildAll(entries);
    assert.equal(d.length, 2);
    assert.deepStrictEqual(d[0], { idx: 0 });
    assert.deepStrictEqual(d[1], { idx: 1 });
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
    assert.deepStrictEqual(d[0], { idx: 0 });                                         // user
    assert.deepStrictEqual(d[1], { idx: 1 });                                         // thinking
    assert.deepStrictEqual(d[2], { type: 'group', tool_name: 'Read', start: 2, end: 3 });   // Read x2
    assert.deepStrictEqual(d[3], { type: 'group', tool_name: 'Edit', start: 4, end: 6 });   // Edit x3
    assert.deepStrictEqual(d[4], { idx: 7 });                                         // asst
    assert.deepStrictEqual(d[5], { idx: 8 });                                         // single Bash
  });

  it('all groupable tools are recognized', () => {
    const tools = ['Bash', 'Read', 'Edit', 'Grep', 'Glob'];
    for (const t of tools) {
      const entries = [tool(t, t + '1'), tool(t, t + '2')];
      const d = buildAll(entries);
      assert.equal(d.length, 1, t + ' should be grouped');
      assert.equal(d[0].type, 'group');
      assert.equal(d[0].tool_name, t);
    }
  });
});

// ── Suite 2: appendOne ───────────────────────────────────────────────

describe('appendOne', () => {
  it('append to empty display', () => {
    const entries = [user('hi')];
    const d = [];
    appendOne(d, entries);
    assert.deepStrictEqual(d, [{ idx: 0 }]);
  });

  it('append non-groupable entry', () => {
    const entries = [user('hi'), asst('hello')];
    const d = [{ idx: 0 }];
    appendOne(d, entries);
    assert.deepStrictEqual(d, [{ idx: 0 }, { idx: 1 }]);
  });

  it('extend existing group with same tool', () => {
    const entries = [tool('Bash', 'b1'), tool('Bash', 'b2'), tool('Bash', 'b3')];
    const d = [{ type: 'group', tool_name: 'Bash', start: 0, end: 1 }];
    appendOne(d, entries);
    assert.equal(d.length, 1);
    assert.equal(d[0].end, 2);
  });

  it('promote single to group when same groupable tool appended', () => {
    const entries = [tool('Read', 'r1'), tool('Read', 'r2')];
    const d = [{ idx: 0 }];
    appendOne(d, entries);
    assert.equal(d.length, 1);
    assert.deepStrictEqual(d[0], { type: 'group', tool_name: 'Read', start: 0, end: 1 });
  });

  it('different tool breaks into new single', () => {
    const entries = [tool('Bash', 'b1'), tool('Read', 'r1')];
    const d = [{ idx: 0 }];
    appendOne(d, entries);
    assert.equal(d.length, 2);
    assert.deepStrictEqual(d[1], { idx: 1 });
  });

  it('non-groupable tool_use not promoted', () => {
    const entries = [tool('Agent', 'a1'), tool('Agent', 'a2')];
    const d = [{ idx: 0 }];
    appendOne(d, entries);
    assert.equal(d.length, 2);
    assert.deepStrictEqual(d[1], { idx: 1 });
  });

  it('mutates display in place', () => {
    const entries = [user('x'), user('y')];
    const d = [{ idx: 0 }];
    const result = appendOne(d, entries);
    assert.strictEqual(result, d);
    assert.equal(d.length, 2);
  });

  it('incremental appendOne matches buildAll', () => {
    const entries = [
      user('go'),
      tool('Bash', 'b1'), tool('Bash', 'b2'),
      asst('ok'),
      tool('Edit', 'e1'), tool('Edit', 'e2'), tool('Edit', 'e3'),
      user('done'),
    ];

    // Build incrementally
    const incremental = [];
    for (let i = 0; i < entries.length; i++) {
      appendOne(incremental, entries.slice(0, i + 1));
    }

    // Build all at once
    const full = buildAll(entries);

    assert.deepStrictEqual(incremental, full);
  });
});

// ── Suite 3: resolve ─────────────────────────────────────────────────

describe('resolve', () => {
  it('single descriptor resolves to entry reference', () => {
    const entries = [user('hi'), asst('hello')];
    const resolved = resolve({ idx: 0 }, entries);
    assert.equal(resolved.type, 'user');
    assert.equal(resolved.content, 'hi');
  });

  it('group descriptor resolves to tool_group', () => {
    const entries = [tool('Bash', 'b1'), tool('Bash', 'b2'), tool('Bash', 'b3')];
    const resolved = resolve({ type: 'group', tool_name: 'Bash', start: 0, end: 2 }, entries);
    assert.equal(resolved.type, 'tool_group');
    assert.equal(resolved.tool_name, 'Bash');
    assert.equal(resolved.items.length, 3);
    assert.equal(resolved.timestamp, entries[0].timestamp);
  });

  it('single resolves to original reference (no copy)', () => {
    const entries = [user('hi')];
    const resolved = resolve({ idx: 0 }, entries);
    assert.strictEqual(resolved, entries[0]);
  });

  it('group items are slice references from source array', () => {
    const entries = [tool('Read', 'r1'), tool('Read', 'r2')];
    const resolved = resolve({ type: 'group', tool_name: 'Read', start: 0, end: 1 }, entries);
    assert.strictEqual(resolved.items[0], entries[0]);
    assert.strictEqual(resolved.items[1], entries[1]);
  });
});

// ── Suite 4: entry mutation ──────────────────────────────────────────

describe('entry mutation', () => {
  it('mutation on source entry is visible through descriptor', () => {
    const entries = [user('original')];
    const d = buildAll(entries);
    // Mutate the source entry (simulating dictation rewrite)
    entries[0].content = 'rewritten';
    entries[0].rewritten = true;
    const resolved = resolve(d[0], entries);
    assert.equal(resolved.content, 'rewritten');
    assert.equal(resolved.rewritten, true);
  });

  it('display array unchanged after source mutation', () => {
    const entries = [tool('Bash', 'b1'), tool('Bash', 'b2')];
    const d = buildAll(entries);
    const before = JSON.stringify(d);
    // Mutate source
    entries[0].input = { command: 'ls -la' };
    entries[1].input = { command: 'pwd' };
    const after = JSON.stringify(d);
    assert.equal(before, after, 'display descriptors should not change when entries are mutated');
  });
});
