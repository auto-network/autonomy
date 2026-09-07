/*
 * Operator-visible outcomes for root-warm unlock steps.
 *
 * ONE surfacing mechanism for every step an unlock runs under the open root
 * — the vault wake, and the per-org steps of the root-step runner in
 * network-signon.mjs (rekey, serve-cert, binding, checkpoint). A step that
 * fails silently is the 2026-09-06 class of incident twice over: a vault
 * that never came up behind a completed sign-in (auto-uhdxm), and a
 * membership seed-mint that never ran because it sat on a code path the
 * unlock never took (auto-ujrh7). In both, the operator's evidence was a
 * screen that looked fine.
 *
 * A failure lands in three places, because they answer different questions:
 *
 *   console.error            — the developer looking right now,
 *   POST /api/identity/ceremony-error — the durable capped log
 *                              (autonomy.identity.client-error) for anyone
 *                              reading afterward,
 *   sessionStorage           — the SHELL (identity-indicator.js) renders it
 *                              in the identity panel, which survives the
 *                              unlock page's post-sign-in navigation. This
 *                              is the one an operator actually sees without
 *                              opening DevTools.
 *
 * Storage holds a JSON object keyed by step (and org, when the step is
 * per-org) so several failures coexist and each clears on its own next
 * success. Reporting is best-effort throughout: diagnostics must never be
 * the reason a ceremony fails.
 */

//: sessionStorage key holding {entry: message}. The shell reads this string
//: literally (it loads as a classic script and cannot import) — keep the two
//: in step if it ever changes.
export const STEP_FAILURES_STORAGE_KEY = 'autonomy.unlock.step-failures';

//: What each step was trying to do, in the operator's terms.
const STEP_LABELS = {
  'vault-wake': 'the vault did not come up',
  'checkpoint': 'the membership checkpoint was not recorded',
  'serve-cert': 'the serving certificate was not renewed',
  'binding': 'the registry binding was not renewed',
  'rekey': 'the organization key was not rotated',
};

//: What it costs the operator until the step succeeds.
const STEP_CONSEQUENCES = {
  'vault-wake': 'Stored secrets stay locked until an unlock completes this step.',
  'checkpoint': 'This organization stays invisible to the registry until this succeeds.',
  'serve-cert': 'Serving keeps running on the existing certificate until it expires.',
  'binding': 'The registry binding keeps counting down to expiry.',
  'rekey': 'The organization key stays on its current generation.',
};

//: Reason-slug prefix -> phrase. Longest-meaningful first: 'personal-root-public-key'
//: must win over 'personal-root'.
const REASON_PHRASES = [
  ['anchor-inventory', 'the vault anchor inventory could not be read'],
  ['personal-root-public-key', 'the personal identity record has no root public key'],
  ['personal-root', 'the personal identity record could not be read'],
  ['anchor-race', 'the vault anchor inventory changed mid-ceremony'],
  ['anchor-enroll', 'the root anchor could not be enrolled'],
  ['root-class', 'the root policy class could not be created'],
  ['ledger-no-genesis', 'the ledger database is present but holds no genesis —'
    + ' the wrong store may be resolving, or the data is damaged; do NOT re-found'],
  ['not-founded', 'this identity’s ledger is not founded, so nothing can be delegated'],
  ['heads', 'the ledger heads could not be read'],
  ['delegate', 'the storage delegate grant was refused by the ledger'],
  ['vault-keys', 'the dashboard refused the vault key material'],
];

function reasonPhrase(slug) {
  const match = REASON_PHRASES.find(([prefix]) => slug.startsWith(prefix));
  return match ? match[1] : 'an unexpected step failed';
}

/** The entry key a step's message is stored under. Per-org steps get one each. */
export function stepEntryKey(step, org) {
  return org ? `${step}@${org}` : String(step);
}

/**
 * One operator-legible sentence for a failed step.
 *
 * Ends with a bracketed `[step/reason]` so the machine-readable slug is
 * always recoverable from a screenshot — the operator reports the sentence,
 * and whoever debugs it gets the exact gate for free.
 */
export function describeStepFailure(step, reason, org) {
  const slug = String(reason || 'unknown');
  const label = STEP_LABELS[step] || `the ${step} step did not complete`;
  const scope = org ? ` (${org})` : '';
  const status = /-(\d{3})$/.exec(slug);
  const consequence = STEP_CONSEQUENCES[step]
    || 'Sign in again once the cause is fixed.';
  return label.charAt(0).toUpperCase() + label.slice(1) + scope + ': '
    + reasonPhrase(slug) + (status ? ` (HTTP ${status[1]})` : '') + '. '
    + consequence + ' [' + step + '/' + slug + ']';
}

function loadFailures(storage) {
  try {
    const raw = storage.getItem(STEP_FAILURES_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    return (parsed && typeof parsed === 'object') ? parsed : {};
  } catch (e) {
    return {};
  }
}

function saveFailures(storage, failures) {
  if (Object.keys(failures).length) {
    storage.setItem(STEP_FAILURES_STORAGE_KEY, JSON.stringify(failures));
  } else {
    storage.removeItem(STEP_FAILURES_STORAGE_KEY);
  }
}

/**
 * Classify a step result. Accepts the shapes the callers already produce:
 * `{ready}` (wakeVault), `{ok}`, and `{status: 'ran'|'skipped'|'failed'}`
 * (the root-step runner), so no caller has to reshape its result to be
 * reportable.
 *
 * A SKIPPED step is neither: it did not run, so it neither proves the
 * problem gone nor adds a new one, and its previous message stands.
 */
export function classifyStepResult(result) {
  if (!result || typeof result !== 'object') return 'failed';
  if (result.status === 'skipped') return 'skipped';
  if (result.status === 'failed') return 'failed';
  if (result.ok === false || result.ready === false) return 'failed';
  if (result.ok === true || result.ready === true || result.status === 'ran') {
    return 'succeeded';
  }
  return 'skipped';
}

/**
 * Make one step's outcome visible. Call it for EVERY outcome, not only
 * failures: a success is what clears the previous failure's message.
 *
 * @param {string} step   'vault-wake' | 'checkpoint' | 'serve-cert' | …
 * @param {object} result `{ready}` / `{ok, reason}` / `{status, reason}`
 * @param {object} [options] `{fetchImpl, org}` — org names the per-org step.
 * @returns {string} the classification ('succeeded' | 'skipped' | 'failed').
 */
export function reportStepOutcome(step, result, options = {}) {
  const { fetchImpl = fetch, org = null } = options;
  const verdict = classifyStepResult(result);
  const storage = (typeof sessionStorage !== 'undefined') ? sessionStorage : null;
  const entry = stepEntryKey(step, org);
  try {
    if (verdict === 'skipped') return verdict;
    if (verdict === 'succeeded') {
      if (storage) {
        const failures = loadFailures(storage);
        if (entry in failures) {
          delete failures[entry];
          saveFailures(storage, failures);
        }
      }
      return verdict;
    }
    const reason = (result && result.reason) || 'unknown';
    const message = describeStepFailure(step, reason, org);
    if (typeof console !== 'undefined' && console.error) {
      console.error('unlock step failed:', message);
    }
    if (storage) {
      const failures = loadFailures(storage);
      failures[entry] = message;
      saveFailures(storage, failures);
    }
    Promise.resolve(fetchImpl('/api/identity/ceremony-error', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ceremony: step,
        action: String(reason),
        name: 'UnlockStepFailure',
        message,
        stack: '',
        context: org ? { org } : {},
      }),
    })).catch(() => {});
  } catch (e) { /* diagnostics must never break the ceremony */ }
  return verdict;
}
