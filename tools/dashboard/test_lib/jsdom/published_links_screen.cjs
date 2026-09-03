const { JSDOM } = require('jsdom');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const assert = require('node:assert/strict');

const orgSettings = readFileSync(resolve(__dirname, '../../static/js/org-settings.js'), 'utf8');
const published = readFileSync(resolve(__dirname, '../../static/js/published-links.js'), 'utf8');
const settle = () => new Promise((r) => setTimeout(r, 0));

async function main() {
  const dom = new JSDOM('<!doctype html><body></body>', {runScripts:'dangerously', url:'https://dashboard.test/'});
  const w = dom.window;
  w.matchMedia = () => ({matches:false,addListener(){},removeListener(){},addEventListener(){},removeEventListener(){}});
  const calls = [];
  const data = {service_warning:'Automatic renewal failed; certificate expires in 2 days.',services:[{reservation_id:'r1',origin:'https://oss-insights.persona-long.serve.auto.network',persona_label:'persona-long',app_label:'oss-insights',state:'active',session_title:'OSS Insights UI — repository analytics dashboard',target:{session_id:'auto-1',port:3000}}],shares:[{token:'a'.repeat(32),target_uuid:'n1',type:'note',title:'Architecture',description:'Relay boundaries',url:'https://relay.test/l/a',platform_url:'/graph/n1',expires_at:null,expired:false}]};
  w.fetch = async (url, options={}) => {
    calls.push([url, options]);
    if (url === '/api/orgs/anchore') return {ok:true,json:async()=>({identity_resolved:{name:'Anchore',favicon:'/static/orgs/anchore.png'}})};
    if (url === '/api/network/published-links') return {ok:true,json:async()=>data};
    if (/\/state$/.test(url)) return {ok:true,json:async()=>({reservation:{}})};
    throw new Error('unexpected fetch '+url);
  };
  for (const source of [orgSettings,published]) { const s=w.document.createElement('script');s.textContent=source;w.document.head.appendChild(s); }
  w.AutonomyOrgSettings.open('anchore'); await settle();
  w.document.querySelector('[data-testid="orgset-rail-published-links"]').click(); await settle(); await settle();
  assert.equal(w.document.querySelector('.orgset-title').textContent.replace(/\s+/g,' ').trim(),'Anchore–Published Services & Links');
  assert.equal(w.document.querySelector('.pl-session-title').textContent,'OSS Insights UI — repository analytics dashboard');
  assert.equal(w.document.querySelector('.pl-terminal').textContent,'auto-1');
  assert.equal(w.document.querySelector('.pl-terminal').textContent.includes('3000'),false,'internal port leaked into approved card');
  assert.match(w.document.querySelector('.pl-notice').textContent,/expires in 2 days/);
  assert.ok(w.document.querySelector('[data-action="share"]'));
  assert.ok(w.document.querySelector('[data-action="visit"]'));
  w.document.querySelector('[data-action="stop"]').click();
  assert.match(w.document.querySelector('.pl-detail.open').textContent,/Stop this Service/);
  w.document.querySelector('[data-action="cancel"]').click();
  w.document.querySelector('[data-action="toggle"]').click(); await settle(); await settle();
  const transition = calls.find(([url]) => /\/state$/.test(url));
  assert.equal(JSON.parse(transition[1].body).state,'paused');
  w.document.querySelector('[data-tab="shares"]').click();
  assert.equal(w.document.querySelector('.pl-share-title').textContent,'Architecture');
  assert.equal(w.document.querySelector('.pl-expiry'),null,'non-expiring share rendered a badge');
  let viewed = null;
  w.navigateTo = (url) => { viewed = url; };
  assert.equal(w.document.querySelector('[data-action="visit"]').getAttribute('aria-label'),'Open public link');
  w.document.querySelector('[data-action="view"]').click();
  assert.equal(viewed, '/graph/n1', 'View did not use the in-platform target');
  assert.equal(w.document.querySelector('[data-testid="org-settings"]'), null,
    'View left the Settings dialog covering its destination');
  console.log('PASS: approved Published Services & Links view state and transitions');
}
main().catch((e)=>{console.error(e);process.exit(1);});
