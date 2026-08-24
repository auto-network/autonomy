// Markdown link policy: ordinary web URLs open separately, the explicitly
// allowlisted Shortcuts run URL survives as a direct app handoff, and unsafe
// or unknown schemes never become actionable.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const { JSDOM } = require("jsdom");

const dom = new JSDOM("<!doctype html><html><head></head><body></body></html>", {
  runScripts: "dangerously",
  url: "https://dashboard.example/session/test",
});
const { window } = dom;
let markdownDirective = null;

window.Alpine = {
  directive: function (name, callback) {
    if (name === "markdown") markdownDirective = callback;
  },
};
window.hljs = { highlightElement: function () {} };
window.navigateTo = function () {};

function inject(relativePath) {
  const script = window.document.createElement("script");
  script.textContent = readFileSync(resolve(__dirname, relativePath), "utf8");
  window.document.head.appendChild(script);
}

inject("../../static/vendor/marked-15.0.12.min.js");
inject("../../static/vendor/purify-3.4.12.min.js");
inject("../../static/js/markdown.js");
window.document.dispatchEvent(new window.Event("alpine:init"));
assert.equal(typeof markdownDirective, "function");

const pairing = "shortcuts://run-shortcut?name=Autonomy%20Capture&input=text&text=https%3A%2F%2Fdashboard.example";
const markdown = [
  `[Pair Autonomy Capture](${pairing})`,
  "[Apple](https://support.apple.com/)",
  "[Unsafe](javascript:alert(1))",
  "[Unknown](otherapp://launch)",
  "[Wrong Shortcuts command](shortcuts://create-shortcut)",
  `<img alt="Passive launch" src="${pairing}">`,
].join("\n\n");

const host = window.document.createElement("div");
markdownDirective(
  host,
  { expression: "message", modifiers: [] },
  {
    effect: function (callback) { callback(); },
    evaluate: function () { return markdown; },
  },
);

function link(label) {
  return [...host.querySelectorAll("a")].find((a) => a.textContent === label);
}

const pair = link("Pair Autonomy Capture");
assert.equal(pair.getAttribute("href"), pairing);
assert.equal(pair.getAttribute("target"), null);
assert.equal(pair.getAttribute("data-external-app"), "shortcuts");
assert.equal(pair.getAttribute("title"), "Open in Shortcuts");

const web = link("Apple");
assert.equal(web.getAttribute("href"), "https://support.apple.com/");
assert.equal(web.getAttribute("target"), "_blank");
assert.equal(web.getAttribute("rel"), "noopener noreferrer");

assert.equal(link("Unsafe").hasAttribute("href"), false);
assert.equal(link("Unknown").hasAttribute("href"), false);
assert.equal(link("Wrong Shortcuts command").hasAttribute("href"), false);
assert.equal(host.querySelector('img[alt="Passive launch"]').hasAttribute("src"), false);

console.log("markdown external-link policy -> PASS");
