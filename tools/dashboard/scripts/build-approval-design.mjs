/* Bundle the production control into a self-contained Design Studio document.
 * No second rendering implementation: the module body is included unchanged.
 * Run with OUTPUT.html; --watch rebuilds when the production source changes.
 */
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
const source = fileURLToPath(new URL('../static/js/components/approval-experiment.js', import.meta.url));
const output = process.argv[2];
if (!output) throw new Error('Supply the Design Studio HTML output path.');
function build() {
  const code = fs.readFileSync(source, 'utf8').replace(/^export /gm, '');
  const html = `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Approval</title><style>body{margin:0;background:#080b12}</style></head><body><main id="approval-host" x-data="window.FIXTURE"></main><script type="module">\n${code}\nconst control=mountApprovalExperiment(document.getElementById('approval-host'),window.FIXTURE||{kind:'central'});window.addEventListener('fixture-state-change',event=>control.update(event.detail.data));\n</script></body></html>`;
  fs.writeFileSync(output, html);
  console.log('Built Design Studio from production approval-experiment.js');
}
build();
if (process.argv.includes('--watch')) fs.watch(source, build);
