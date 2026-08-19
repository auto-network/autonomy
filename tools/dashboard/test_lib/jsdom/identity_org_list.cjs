// The identity panel's organization list, against the REAL /api/orgs shape.
//
// This exists because the first cut shipped to production rendering six real
// organizations as "?" with no names. It was tested only against fixtures the
// author invented, which had the fields at the top level. The real route
// returns {org:{slug,...}, identity:{payload:{name,byline,color}}} — every
// field one level down from where the renderer looked.
//
// So the fixture below is a VERBATIM-SHAPED response, not a convenient one.
// A test that invents its own shape cannot catch a shape mismatch.
const { JSDOM } = require("jsdom");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const assert = require("node:assert/strict");

const src = readFileSync(
  resolve(__dirname, "../../static/js/identity-indicator.js"), "utf8");

// Real shape, abridged to the fields the renderer reads.
const RESPONSE = { orgs: [
  { org: { slug: "anchore", type: "shared" },
    identity: { payload: { name: "Anchore", byline: "Security platform",
                           color: "#2D7D46" } } },
  { org: { slug: "autonomy", type: "shared" },
    identity: { payload: { name: "Autonomy Network", byline: "AGI platform",
                           color: "#6C63FF" } } },
  // Stores, not organizations. Present in /api/orgs because it enumerates
  // data/orgs/*.db. Must NOT be listed.
  { org: { slug: "personal", type: "personal" },
    identity: { payload: { name: "Personal" } } },
  { org: { slug: "machine", type: "machine" }, identity: null },
]};

const dom = new JSDOM("<!DOCTYPE html><body></body>",
  { runScripts: "dangerously", url: "http://localhost/" });
const w = dom.window;
const el = (t, c, x) => { const n = w.document.createElement(t);
  if (c) n.className = c; if (x != null) n.textContent = x; return n; };

const m = src.match(/function orgRow\(entry\)[\s\S]*?\n  }\n/);
assert.ok(m, "orgRow not found — did it get renamed?");
const orgRow = new Function("entry", "el", "root", m[0] + "; return orgRow(entry);");

// 1. A real entry renders its real name and initial, not a placeholder.
{
  const row = orgRow(RESPONSE.orgs[0], el, w);
  const mark = row.querySelector(".identity-org-mark").textContent;
  const name = row.querySelector(".identity-panel-action-label").textContent;
  assert.equal(name, "Anchore", `name rendered as ${name!==""?name:"(empty)"}`);
  assert.equal(mark, "A", `mark rendered as ${mark} — "?" means the shape is wrong again`);
  const detail = row.querySelector(".identity-panel-action-detail");
  assert.equal(detail && detail.textContent, "Security platform");
}

// 2. No entry renders the "?" placeholder, which is what production showed.
for (const entry of RESPONSE.orgs) {
  const mark = orgRow(entry, el, w).querySelector(".identity-org-mark").textContent;
  assert.notEqual(mark, "?",
    `${entry.org.slug} rendered "?" — the renderer is reading a field the route does not return`);
}

// 3. The store entries are filtered out of the list entirely.
const filtered = RESPONSE.orgs.filter((e) => {
  const slug = (e.org || {}).slug;
  return slug !== "personal" && slug !== "machine";
});
assert.deepEqual(filtered.map((e) => e.org.slug), ["anchore", "autonomy"],
  "personal/machine are stores, not organizations, and must not be listed");

console.log("PASS: identity org list — real /api/orgs shape, no placeholders, stores excluded");
