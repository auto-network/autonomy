// The global toast stack is fixed to the viewport. On notched devices, both
// offsets must include the platform safe area so messages remain readable.

const { it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const BASE_TEMPLATE = path.join(REPO_ROOT, 'tools/dashboard/templates/base.html');

it('positions the global toast stack inside mobile safe-area insets', () => {
  const html = fs.readFileSync(BASE_TEMPLATE, 'utf8');
  const start = html.indexOf('#toast-container {');
  const end = html.indexOf('    .toast {', start);
  const toastCss = html.slice(start, end);

  assert.match(toastCss, /top: calc\(1rem \+ env\(safe-area-inset-top, 0px\)\);/);
  assert.match(toastCss, /right: calc\(1rem \+ env\(safe-area-inset-right, 0px\)\);/);
});
