/* Build a classic-script test view of the production ES modules.
 *
 * Production loads autonet.js as a module and imports relaykit-core.js. The
 * older unit harnesses intentionally evaluate the composition with
 * new Function/vm so they can inject browser primitives. Compose the same two
 * source files into one closure for those harnesses; never maintain a second
 * implementation merely to keep a test loader convenient.
 */
"use strict";

const fs = require("fs");
const path = require("path");

const HERE = __dirname;
const CORE = path.join(
  HERE, "..", "..", "..", "dashboard", "static", "js", "lib",
  "relaykit-core.js",
);
const AUTONET = path.join(
  HERE, "..", "..", "registry", "bootloader", "autonet.js",
);

function loadAutonetTestSource() {
  let core = fs.readFileSync(CORE, "utf8");
  core = core
    .replace(/^export function /gm, "function ")
    .replace(/^export async function /gm, "async function ")
    .replace(/^export class /gm, "class ");
  const coreClosure = [
    "const __relaykitCore = (() => {",
    core,
    "return { SecureChannel, canonicalJson, openSocket, performHandshake, sendOp };",
    "})();",
  ].join("\n");

  let composition = fs.readFileSync(AUTONET, "utf8");
  composition = composition.replace(
    /import \{[\s\S]*?\} from "\.\/relaykit-core\.js";\n\n/,
    "const { SecureChannel, canonicalJson, openSocket, performHandshake } = __relaykitCore;\n\n",
  );
  return `${coreClosure}\n${composition}`;
}

module.exports = { loadAutonetTestSource };
