'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const fs = require('node:fs');
const path = require('node:path');
function setup() {
  const dom = new JSDOM('<div id="host"></div><button id="after">After</button>', {url:'https://example.test', runScripts:'outside-only'});
  dom.window.eval(fs.readFileSync(path.join(__dirname, '../static/js/org-picker.js'), 'utf8'));
  const w = dom.window, host = w.document.querySelector('#host');
  w.HTMLElement.prototype.getClientRects = () => [{left:0,top:0,right:100,bottom:40}];
  w.HTMLElement.prototype.getBoundingClientRect = () => ({left:10,top:10,right:110,bottom:50,width:100,height:40});
  const orgs = [{slug:'alpha', name:'Alpha', favicon:'/alpha.png', color:'#123456'}, {slug:'beta',name:'Beta'}];
  return {dom,w,host,orgs,picker:w.OrgPicker};
}
test('normalizes resolved and flat identities, deduplicates, rejects unsafe icon URLs', () => {
  const {dom,picker} = setup();
  const rows = picker.normalize([{org:{slug:'a'},identity_resolved:{name:'Real',favicon:'/a.png'},identity:{payload:{name:'Old'}}},{slug:'a'},{slug:'b',favicon:'javascript:alert(1)'},{}]);
  assert.equal(rows.length,2); assert.equal(rows[0].name,'Real'); assert.equal(rows[0].favicon,'/a.png'); assert.equal(rows[1].favicon,null);
  assert.equal(picker.normalize([{slug:'x',initial:'Two'}])[0].initial,'T');
  assert.equal(picker.normalize([{slug:'x',initial:'ß'}])[0].initial,'S');
  dom.window.close();
});
test('icons load without brand background and fail back to one initial', () => {
  const {dom,w,host,orgs,picker} = setup(); const h=picker.mount(host,{orgs,value:'alpha'});
  const img=host.querySelector('img'), mark=img.parentElement;
  img.dispatchEvent(new w.Event('load')); assert.equal(mark.querySelector('.org-picker-initial').hidden,true);
  img.dispatchEvent(new w.Event('error')); assert.equal(mark.querySelector('.org-picker-initial').hidden,false); assert.equal(mark.querySelector('img'),null);
  assert.equal(mark.textContent,'A'); h.destroy(); dom.window.close();
});
test('selection, keyboard, same-value no-op, stable update and cleanup', () => {
  const {dom,w,host,orgs,picker} = setup(); const changed=[];
  const h=picker.mount(host,{orgs,value:'alpha',onChange:v=>changed.push(v)}), trigger=host.querySelector('button');
  trigger.click(); const menu=w.document.querySelector('.org-picker-menu');
  h.update({orgs,value:'alpha'}); assert.equal(host.querySelector('button'),trigger); assert.equal(menu.hidden,false);
  const key=(el,k)=>el.dispatchEvent(new w.KeyboardEvent('keydown',{key:k,bubbles:true,cancelable:true}));
  key(w.document.activeElement,'End'); key(w.document.activeElement,'Enter');
  assert.deepEqual(changed,['beta']); assert.equal(menu.hidden,true);
  h.update({value:'beta'}); trigger.click(); key(w.document.activeElement,'Enter'); assert.equal(changed.length,1);
  trigger.click(); key(w.document.activeElement,'Escape'); assert.equal(w.document.activeElement,trigger);
  h.destroy(); assert.equal(w.document.querySelector('.org-picker-menu'),null);
  for(let i=0;i<50;i++) picker.mount(host,{orgs,value:'alpha'}).destroy();
  assert.equal(w.document.querySelectorAll('.org-picker-menu').length,0); dom.window.close();
});
test('empty, stale, All and hostile labels never fabricate identity or HTML', () => {
  const {dom,w,host,picker}=setup(); const h=picker.mount(host,{orgs:[],value:''});
  assert.equal(host.querySelector('button').disabled,true);
  h.update({value:'unknown'}); assert.match(host.textContent,/unknown/);
  h.update({value:'ß-missing'}); assert.equal(host.querySelector('.org-picker-initial').textContent,'S');
  h.update({orgs:[{slug:'a',name:'<img onerror=alert(1)>'}],allowAll:true,value:''});
  assert.match(host.textContent,/All organizations/); host.querySelector('button').click();
  assert.match(w.document.querySelector('.org-picker-menu').textContent,/<img onerror/); assert.equal(w.document.querySelector('.org-picker-menu img'),null);
  h.destroy(); dom.window.close();
});

test('one open instance, unique popup IDs, removed focus and empty All inventory', () => {
  const {dom,w,host,orgs,picker}=setup();
  const h=picker.mount(host,{orgs,value:'alpha'});
  const other=w.document.createElement('div');w.document.body.append(other);
  const h2=picker.mount(other,{orgs,value:'beta'});
  const a=host.querySelector('button'), b=other.querySelector('button');
  assert.notEqual(a.getAttribute('aria-controls'),b.getAttribute('aria-controls'));
  a.click(); b.click();
  assert.equal(a.getAttribute('aria-expanded'),'false'); assert.equal(b.getAttribute('aria-expanded'),'true');
  h2.update({orgs:[orgs[0]],value:'alpha'});
  assert.equal(w.document.activeElement.dataset.slug,'alpha');
  h2.update({orgs:[],allowAll:true,value:''});assert.equal(b.disabled,true);
  assert.equal(b.getAttribute('aria-expanded'),'false');
  h.destroy();h2.destroy();dom.window.close();
});

test('late icon events cannot replace updated identity, and URL policy is explicit', () => {
  const {dom,w,host,orgs,picker}=setup(); const h=picker.mount(host,{orgs,value:'alpha'});
  const old=host.querySelector('img');h.update({value:'beta'});
  old.dispatchEvent(new w.Event('load'));old.dispatchEvent(new w.Event('error'));
  assert.match(host.textContent,/Beta/); assert.equal(host.querySelector('img'),null);
  for(const url of ['data:image/png;base64,AA','file:///tmp/a','blob:https://example.test/a','javascript:alert(1)']) {
    assert.equal(picker.normalize([{slug:'a',favicon:url}])[0].favicon,null);
  }
  for(const url of ['/a.png','icons/a.png','../a.png','//example.test/a.png','https://example.test/a.png']) {
    assert.equal(picker.normalize([{slug:'a',favicon:url}])[0].favicon,url);
  }
  h.destroy();dom.window.close();
});

test('scrolling an open picker offscreen closes it without trapping focus', () => {
  const {dom,w,host,orgs,picker}=setup();const h=picker.mount(host,{orgs,value:'alpha'});
  const trigger=host.querySelector('button');trigger.click();
  assert.equal(w.document.activeElement.getAttribute('role'),'option');
  trigger.getBoundingClientRect=()=>({left:10,right:110,top:-80,bottom:-40,width:100,height:40});
  w.dispatchEvent(new w.Event('scroll'));
  assert.equal(trigger.getAttribute('aria-expanded'),'false');assert.equal(w.document.activeElement,trigger);
  h.destroy();dom.window.close();
});
