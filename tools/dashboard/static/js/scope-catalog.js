/* Plain-language names for the ledger scopes the platform enforces —
 * the JavaScript twin of tools/dashboard/scope_catalog.py.
 *
 * The table between the CATALOG-BEGIN / CATALOG-END markers is a JSON
 * literal; the Python side's test reads it verbatim and proves the two never
 * drift. Edit both, or the test fails. Only enforced scopes belong here.
 *
 * Usable as an ES module (`import { describeScope } from
 * '/static/js/scope-catalog.js'`) and, once loaded, as
 * `window.AutonomyScopeCatalog` for the classic-script screens.
 */

/* CATALOG-BEGIN */
const CATALOG = [
  {
    "pattern": "*",
    "family": "everything",
    "label": "Everything",
    "sentence": "Every authority the organization can confer, now and later.",
    "enforced_by": "ledger fold (UNIVERSE)"
  },
  {
    "pattern": "role:define",
    "family": "roles",
    "label": "Define roles",
    "sentence": "Define or revise roles, within the authority you may delegate.",
    "enforced_by": "ledger fold _h_role_define"
  },
  {
    "pattern": "role:grant:*",
    "family": "roles",
    "label": "Approve and grant any role",
    "sentence": "Approve joiners for, and grant or revoke, every role.",
    "enforced_by": "ledger fold _h_role_grant / claim approvals"
  },
  {
    "pattern": "role:grant:{role}",
    "family": "roles",
    "label": "Approve and grant {role}",
    "sentence": "Approve joiners for {role}, and grant or revoke it.",
    "enforced_by": "ledger fold _h_role_grant / claim approvals"
  },
  {
    "pattern": "invite:*",
    "family": "invitations",
    "label": "Invite people as any role",
    "sentence": "Mint invitations for every role.",
    "enforced_by": "ledger fold _h_invite; POST /api/network/ledger/invite"
  },
  {
    "pattern": "invite:{role}",
    "family": "invitations",
    "label": "Invite people as {role}",
    "sentence": "Mint invitations that admit people as {role}.",
    "enforced_by": "ledger fold _h_invite; POST /api/network/ledger/invite"
  },
  {
    "pattern": "link:publish",
    "family": "share links",
    "label": "Publish share links",
    "sentence": "Publish organization content behind a share link.",
    "enforced_by": "link_approvals (org_authority.authorize)"
  },
  {
    "pattern": "link:revoke",
    "family": "share links",
    "label": "Revoke share links",
    "sentence": "Take a published share link down.",
    "enforced_by": "link_approvals (org_authority.authorize)"
  },
  {
    "pattern": "link:read",
    "family": "share links",
    "label": "Read share links",
    "sentence": "Read organization content served behind share links.",
    "enforced_by": "link serving grants"
  },
  {
    "pattern": "membership:checkpoint",
    "family": "membership",
    "label": "Sign membership checkpoints",
    "sentence": "Sign the committed-membership checkpoints the registry trusts.",
    "enforced_by": "membership_commitment (CHECKPOINT_SCOPE)"
  }
];
/* CATALOG-END */

const ROLE_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;

function fill(entry, role) {
  const out = {
    scope: role == null ? entry.pattern : entry.pattern.replace('{role}', role),
    family: entry.family,
    label: entry.label,
    sentence: entry.sentence,
    enforced_by: entry.enforced_by,
    unknown: false,
  };
  if (role != null) {
    out.label = entry.label.replace('{role}', role);
    out.sentence = entry.sentence.replace('{role}', role);
    out.role = role;
  }
  return out;
}

/** Resolve one concrete scope or pattern to its catalog entry; unknown
 * scopes come back as `{unknown: true, scope}` so a caller renders them as
 * a mono chip rather than hiding them. Mirrors describe_scope in Python. */
export function describeScope(scope) {
  if (typeof scope !== 'string' || !scope) return { unknown: true, scope: scope };
  for (const entry of CATALOG) {
    if (!entry.pattern.includes('{role}') && entry.pattern === scope) return fill(entry, null);
  }
  for (const entry of CATALOG) {
    if (!entry.pattern.includes('{role}')) continue;
    const prefix = entry.pattern.split('{role}')[0];
    if (scope.startsWith(prefix)) {
      const role = scope.slice(prefix.length);
      if (ROLE_NAME.test(role)) return fill(entry, role);
    }
  }
  return { unknown: true, scope: scope };
}

export function describeScopeSet(scopes) {
  return (scopes || []).map(describeScope);
}

/** The families in display order, each with its catalog entries. */
export function families() {
  const order = [];
  const byFamily = new Map();
  for (const entry of CATALOG) {
    if (!byFamily.has(entry.family)) { byFamily.set(entry.family, []); order.push(entry.family); }
    byFamily.get(entry.family).push(entry);
  }
  return order.map((family) => ({ family, entries: byFamily.get(family) }));
}

export function catalog() {
  return CATALOG.map((entry) => ({ ...entry }));
}

if (typeof window !== 'undefined') {
  window.AutonomyScopeCatalog = { describeScope, describeScopeSet, families, catalog };
}

export default describeScope;
