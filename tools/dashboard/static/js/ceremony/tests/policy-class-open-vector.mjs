import fs from 'node:fs';
import { openContentKey } from '../policy-class-open.js';

const v = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
try {
  const cek = await openContentKey(v.bundle, v.openers);
  process.stdout.write(JSON.stringify({ ok: true, cek }));
} catch (e) {
  process.stdout.write(JSON.stringify({ ok: false, error: e.message }));
}
