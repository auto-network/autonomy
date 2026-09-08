'use strict';
const {test}=require('node:test'), assert=require('node:assert/strict');
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path');
test('Mission consumes shared picker with resolved icons and removes it inside a mission',async()=>{
  const dom=new JSDOM('<header><div id="app-topbar-slot"></div></header>'+fs.readFileSync(path.join(__dirname,'../page.html'),'utf8'),{url:'https://example.test/mission',runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window;
  w.fetch=async url=>({ok:true,json:async()=>url==='/api/orgs'?{orgs:[
    {org:{slug:'autonomy'},identity_resolved:{name:'Resolved',favicon:'/real.png'},identity:{payload:{name:'Old'}}},
    {org:{slug:'beta'},identity_resolved:{name:'Beta'}},
    {org:{slug:'personal'},identity_resolved:{name:'Personal'}},
  ]}:{missions:[]}});
  w.eval(fs.readFileSync(path.join(__dirname,'../../../static/js/org-picker.js'),'utf8'));
  w.eval(fs.readFileSync(path.join(__dirname,'../page.js'),'utf8'));
  w.eval(fs.readFileSync(path.join(__dirname,'../../../static/vendor/alpine-3.15.12.min.js'),'utf8'));
  await new Promise(r=>setTimeout(r,40));
  const d=w.Alpine.$data(w.document.querySelector('[x-data="missionPage()"]'));
  const trigger=w.document.querySelector('[data-testid=mission-org-select]');
  assert.ok(trigger);assert.equal(trigger.querySelector('img').getAttribute('src'),'/real.png');
  const menu=w.document.getElementById(trigger.getAttribute('aria-controls'));
  assert.equal(menu.querySelector('[data-slug=personal]'),null);
  menu.querySelector('[data-slug=beta]').click();await w.Alpine.nextTick();
  assert.equal(d.org,'beta');assert.equal(w.localStorage.getItem('msn.org'),'beta');
  d.current='mission-one';await w.Alpine.nextTick();
  assert.equal(w.document.querySelector('[data-testid=mission-org-select]'),null);
  assert.equal(menu.isConnected,false);
  await new Promise(r=>setTimeout(r,0));
  w.Alpine.stopObservingMutations();dom.window.close();
});
