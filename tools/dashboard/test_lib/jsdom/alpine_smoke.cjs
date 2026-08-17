// Browser-free smoke test: proves the vendored Alpine build drives reactivity +
// x-for rendering + input events inside jsdom, with no real browser and no
// layout engine. This is the toolchain the dashboard's Category-B UI tests
// (data -> render -> interact -> assert DOM) build on. Layout/positioning
// assertions (getBoundingClientRect) are NOT in scope — jsdom has no layout
// engine and those stay on a real browser.
//
// CommonJS (require) on purpose: NODE_PATH resolves global modules for require
// but NOT for ESM import. jsdom is installed globally in the agent image with
// NODE_PATH=/usr/lib/node_modules (see agents/Dockerfile). Exits 0/1.
const { JSDOM } = require("jsdom");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");

const alpine = readFileSync(
  resolve(__dirname, "../../static/vendor/alpine-3.15.12.min.js"), "utf8");

const html = `<!DOCTYPE html><html><head></head><body>
  <div id="app" x-data="{items:['apple','banana','cherry'], q:''}">
    <input id="q" x-model="q">
    <ul id="list"><template x-for="i in items.filter(x=>x.includes(q))" :key="i"><li x-text="i"></li></template></ul>
  </div></body></html>`;

const dom = new JSDOM(html, { runScripts: "dangerously", pretendToBeVisual: true, url: "http://localhost/" });
const { window } = dom;
const s = window.document.createElement("script");
s.textContent = alpine;
window.document.head.appendChild(s);
window.document.dispatchEvent(new window.Event("DOMContentLoaded", { bubbles: true }));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  await sleep(100);
  const before = window.document.querySelectorAll("#list li").length;
  const input = window.document.getElementById("q");
  input.value = "an";
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  await sleep(50);
  const after = [...window.document.querySelectorAll("#list li")].map((l) => l.textContent);
  const ok = before === 3 && after.length === 1 && after[0] === "banana";
  console.log(`initial=${before} afterFilter=${JSON.stringify(after)} -> ${ok ? "PASS" : "FAIL"}`);
  process.exit(ok ? 0 : 1);
})();
