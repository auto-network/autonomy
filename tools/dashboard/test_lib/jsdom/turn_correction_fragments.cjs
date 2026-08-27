// Browser-free regression checks for turn-correction inline fragments.
//
// The Python resolver and browser overlay share conceptually equivalent diff
// logic. Changes here lock punctuation handling behavior in a way that catches
// tokenizer/normalization drift without a full browser.

const { JSDOM } = require("jsdom");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const assert = require("node:assert");

const dom = new JSDOM("<!DOCTYPE html><body></body>", {
  runScripts: "dangerously",
  url: "http://localhost/",
});
const { window } = dom;

const diffSrc = readFileSync(
  resolve(__dirname, "../../static/js/lib/session-diff.js"),
  "utf8",
);
const script = window.document.createElement("script");
script.textContent = diffSrc;
window.document.body.appendChild(script);

if (!window.SessionDiff || typeof window.SessionDiff.fragments !== "function") {
  throw new Error("SessionDiff.fragments is not loaded");
}

assert.strictEqual(
  window.SessionDiff.normalizeCorrectionText("First\\n\\nSecond"),
  "First\n\nSecond",
  "Literal escaped newlines should become paragraph breaks",
);

function collect(frags, kind) {
  return frags.filter((f) => f.kind === kind).map((f) => f.text).join("");
}

function asString(frags) {
  return frags.map((f) => f.text).join("");
}

{
  const frags = window.SessionDiff.fragments("I won’t go", "I won't go");
  assert.deepStrictEqual([...new Set(frags.map((f) => f.kind))].sort(), ["same"], "Apostrophe style variants should normalize to same");
  assert.strictEqual(collect(frags, "delete"), "", "No deleted text for apostrophe-style variant");
  assert.strictEqual(collect(frags, "insert"), "", "No inserted text for apostrophe-style variant");
  assert.strictEqual(asString(frags), "I won’t go", "Rendered text must be preserved");
}

{
  const frags = window.SessionDiff.fragments("Please review this", "Please review this,");
  assert.strictEqual(collect(frags, "insert"), ",", "Comma insertion should be punctuation-only");
  assert.strictEqual(collect(frags, "delete"), "", "Comma insertion should not reclassify the word as deleted");
  assert.strictEqual(asString(frags), "Please review this,", "Text reconstruction should remain unchanged");
}

{
  const frags = window.SessionDiff.fragments("“Hello”", '"Hello"');
  assert.deepStrictEqual([...new Set(frags.map((f) => f.kind))].sort(), ["same"], "Quote-style variants should normalize");
  assert.strictEqual(collect(frags, "delete"), "", "No deletion for quote-style variant");
  assert.strictEqual(collect(frags, "insert"), "", "No insertion for quote-style variant");
  assert.strictEqual(asString(frags), "“Hello”", "Render text should preserve original raw text");
}

{
  const frags = window.SessionDiff.fragments("Please review this", "Please review this ");
  assert.deepStrictEqual([...new Set(frags.map((f) => f.kind))].sort(), ["same"], "Trailing whitespace should be ignored");
  assert.strictEqual(collect(frags, "delete"), "", "No deletion for trailing whitespace");
  assert.strictEqual(collect(frags, "insert"), "", "No insertion for trailing whitespace");
}

console.log("PASS: turn-correction punctuation fragment behavior");
