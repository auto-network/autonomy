// Mermaid → SVG renderer. Reads Mermaid source on stdin (or a file path
// if --in is given), writes SVG to stdout (or --out if given). Used by
// the mermaid-share / mermaid-render shims.
//
// Implementation: puppeteer-core launches the existing agent-browser
// Chromium in headless mode, loads a tiny HTML wrapper that imports the
// vendored mermaid.min.js, calls mermaid.render(), and exfiltrates the
// SVG via page.evaluate. Pure-JS DOM shims (jsdom, linkedom) cannot be
// used because Mermaid's layout calls SVGTextElement.getBBox() for text
// measurement, which only a real layout engine implements.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import puppeteer from 'puppeteer-core';

const __dirname = dirname(fileURLToPath(import.meta.url));
const DEFAULT_CHROMIUM = '/home/agent/.agent-browser/browsers/chrome-148.0.7778.56/chrome';
const MERMAID_PATH = resolve(__dirname, 'node_modules/mermaid/dist/mermaid.min.js');

// Canonical palette — applied automatically when the source uses the
// well-known class names (:::setting, :::file, :::step, :::out, :::err,
// :::fall). Authors don't redeclare classDef in each diagram.
const PALETTE = {
  default: `
classDef setting fill:#dceaff,stroke:#1f4ea8,color:#0b2545,stroke-width:1.4px
classDef file    fill:#fde9c2,stroke:#a86a17,color:#3a2400,stroke-width:1.4px
classDef step    fill:#ffffff,stroke:#222,color:#111,stroke-width:1.4px
classDef out     fill:#d8f0d8,stroke:#1f7a2a,color:#0b3a0b,stroke-width:1.4px
classDef err     fill:#fcd5d3,stroke:#982020,color:#400,stroke-width:1.4px
classDef fall    fill:#fbe9d7,stroke:#a06223,color:#3a1a00,stroke-width:1.4px
`.trim(),
};

function parseArgs(argv) {
  const args = { in: null, out: null, theme: 'default', probe: false, help: false };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--in' || a === '-i') args.in = argv[++i];
    else if (a === '--out' || a === '-o') args.out = argv[++i];
    else if (a === '--theme') args.theme = argv[++i];
    else if (a === '--probe') args.probe = true;
    else if (a === '--help' || a === '-h') args.help = true;
    else if (a.startsWith('--')) {
      console.error(`render.mjs: unknown flag ${a}`);
      process.exit(2);
    } else if (!args.in) args.in = a;
  }
  return args;
}

function usage() {
  return `mermaid-render — Mermaid source → SVG.

Usage:
  echo 'flowchart LR; A --> B' | mermaid-render
  mermaid-render --in diagram.mmd --out diagram.svg
  mermaid-render --probe

Flags:
  --in / -i <path>   Read Mermaid source from file (default: stdin).
  --out / -o <path>  Write SVG to file (default: stdout).
  --theme <name>     Palette preset (default: default).
  --probe            Render a trivial diagram and exit 0 if successful.
  -h, --help         Show this help.

Environment:
  MERMAID_CHROMIUM   Path to a Chromium binary. Default:
                     ${DEFAULT_CHROMIUM}
`;
}

async function readStdin() {
  let buf = '';
  for await (const chunk of process.stdin) buf += chunk;
  return buf;
}

function buildHtml(mermaidJs, source) {
  const escaped = source.replace(/[<>&]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]));
  return `<!doctype html>
<html><head><meta charset="utf-8">
<style>html,body{margin:0;padding:0;background:#fff;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}</style>
</head><body>
<pre id="src" style="display:none">${escaped}</pre>
<div id="out"></div>
<script>${mermaidJs}</script>
<script>
mermaid.initialize({ startOnLoad: false, theme: 'default', securityLevel: 'loose',
  flowchart: { htmlLabels: true, curve: 'basis', useMaxWidth: false, nodeSpacing: 40, rankSpacing: 70 } });
(async () => {
  try {
    const src = document.getElementById('src').textContent;
    const { svg } = await mermaid.render('m', src);
    document.getElementById('out').innerHTML = svg;
    document.body.dataset.ready = '1';
  } catch (e) {
    document.body.dataset.error = String(e && e.message || e);
    document.body.dataset.ready = '1';
  }
})();
</script>
</body></html>`;
}

function injectPalette(source, theme) {
  const palette = PALETTE[theme];
  if (!palette) throw new Error(`unknown theme: ${theme}`);
  if (source.includes('classDef')) return source;  // author already styled
  return `${source.trimEnd()}\n${palette}\n`;
}

async function renderOnce(source, theme) {
  const mermaidJs = readFileSync(MERMAID_PATH, 'utf8');
  const html = buildHtml(mermaidJs, injectPalette(source, theme));
  const chromium = process.env.MERMAID_CHROMIUM || DEFAULT_CHROMIUM;
  const browser = await puppeteer.launch({
    executablePath: chromium,
    headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
  });
  try {
    const page = await browser.newPage();
    await page.setContent(html, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => document.body.dataset.ready === '1', { timeout: 10000 });
    const error = await page.evaluate(() => document.body.dataset.error || null);
    if (error) throw new Error(`mermaid render failed: ${error}`);
    const svg = await page.evaluate(() => {
      const el = document.querySelector('#out svg');
      return el ? el.outerHTML : null;
    });
    if (!svg) throw new Error('no SVG produced');
    return svg;
  } finally {
    await browser.close();
  }
}

async function main() {
  const args = parseArgs(process.argv);
  if (args.help) { process.stdout.write(usage()); return; }
  if (args.probe) {
    try {
      const svg = await renderOnce('flowchart LR\n  A --> B', 'default');
      if (svg && svg.startsWith('<svg')) { console.error('probe ok'); process.exit(0); }
      console.error('probe failed: empty SVG'); process.exit(1);
    } catch (e) { console.error(`probe failed: ${e.message}`); process.exit(1); }
  }
  const source = args.in
    ? readFileSync(args.in, 'utf8')
    : await readStdin();
  if (!source.trim()) { console.error('mermaid-render: no source input'); process.exit(2); }
  const svg = await renderOnce(source, args.theme);
  if (args.out) {
    const { writeFileSync } = await import('node:fs');
    writeFileSync(args.out, svg);
  } else {
    process.stdout.write(svg);
  }
}

main().catch(e => { console.error(`mermaid-render: ${e.message}`); process.exit(1); });
