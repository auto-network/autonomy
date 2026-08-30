/**
 * Sessions launch-menu workspace filtering.
 *
 * Run: node --test tools/dashboard/tests/test_sessions_workspace_filter.js
 */
const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const { SESSIONS_HTML, makeSessionsPage } = require('./sessions_page_harness');

describe('sessions workspace launch menu', () => {
  it('shows every organization when All Orgs is selected', () => {
    const page = makeSessionsPage();
    page.orgGroups = [
      { org: { slug: 'autonomy' }, projects: [{ id: 'autonomy' }] },
      { org: { slug: 'anchore' }, projects: [{ id: 'enterprise-ng' }] },
    ];
    page.selectedOrg = '';

    assert.deepEqual(
      Array.from(page.visibleOrgGroups, (group) => group.org.slug),
      ['autonomy', 'anchore'],
    );
  });

  it('shows only workspaces belonging to the selected organization', () => {
    const page = makeSessionsPage();
    page.orgGroups = [
      { org: { slug: 'autonomy' }, projects: [{ id: 'autonomy' }] },
      { org: { slug: 'anchore' }, projects: [{ id: 'enterprise-v5' }, { id: 'enterprise-ng' }] },
    ];
    page.selectedOrg = 'anchore';

    assert.deepEqual(
      Array.from(page.visibleOrgGroups, (group) => group.org.slug),
      ['anchore'],
    );
    assert.deepEqual(
      Array.from(page.visibleOrgGroups[0].projects, (project) => project.id),
      ['enterprise-v5', 'enterprise-ng'],
    );
  });

  it('keeps Host Terminal independent from the filtered workspace loop', () => {
    const html = fs.readFileSync(SESSIONS_HTML, 'utf8');
    assert.match(html, /x-for="group in visibleOrgGroups"/);
    assert.match(html, /data-testid="host-terminal-option"/);
    assert.ok(
      html.indexOf('data-testid="host-terminal-option"') > html.indexOf('x-for="group in visibleOrgGroups"'),
      'Host Terminal should remain outside and after the filtered workspace groups',
    );
  });
});
