const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = path.join(__dirname, '../static/js/pages/session-viewer.js');
function harness() {
  let factory, init;
  const calls=[];
  const sandbox={window:{},document:{addEventListener(name,fn){if(name==='alpine:init')init=fn;}},
    Alpine:{data(name,fn){factory=fn},store(){return {}}},console,setTimeout,clearTimeout};
  sandbox.fetch=async url=>{calls.push(url);return {ok:true,json:async()=>({pending_approval:h.pending})}};
  const h={pending:null,calls,sandbox};
  sandbox.window.openCentralApproval=async id=>{calls.push(['central',id]);return true};
  sandbox.window.openApprovalOverlay=async id=>{calls.push(['legacy',id]);return true};
  vm.createContext(sandbox);vm.runInContext(fs.readFileSync(source,'utf8'),sandbox);init();
  h.viewer=factory({});h.viewer.sessionKey='session-1';return h;
}
test('discovery never opens; explicit button reopens exact Central attention ID',async()=>{
  const h=harness();h.pending={id:'approval-1',kind:'dashboard_access',attention_id:'recipient-1'};
  await h.viewer._checkPendingApproval();
  assert.equal(h.viewer.pendingApproval.id,'approval-1');
  assert.equal(h.calls.length,1);
  await h.viewer.openPendingApproval();
  assert.deepEqual(h.calls.at(-1),['central','recipient-1']);
});
test('legacy request uses existing opener; resolved request clears the envelope',async()=>{
  const h=harness();h.pending={id:'legacy-1',kind:'link_publish'};
  await h.viewer.openPendingApproval();assert.deepEqual(h.calls.at(-1),['legacy','legacy-1']);
  h.pending=null;await h.viewer._checkPendingApproval();assert.equal(h.viewer.pendingApproval,null);
});
test('late response cannot populate a different session or destroyed viewer',async()=>{
  for(const destroyed of [false,true]) {
    const h=harness();let finish;
    h.sandbox.fetch=()=>new Promise(r=>finish=r);
    const request=h.viewer._checkPendingApproval();
    if(destroyed)h.viewer._approvalDestroyed=true;else h.viewer.sessionKey='session-2';
    finish({ok:true,json:async()=>({pending_approval:{id:'old'}})});await request;
    assert.equal(h.viewer.pendingApproval,null);
  }
});
test('failed refresh preserves pending pointer but does not open an unverified request',async()=>{
  const h=harness();h.viewer.pendingApproval={id:'old'};
  h.sandbox.fetch=async()=>({ok:false});await h.viewer.openPendingApproval();
  assert.equal(h.viewer.pendingApproval.id,'old');assert.equal(h.calls.length,0);
  assert.match(h.viewer.approvalOpenError,/Could not load/);
});
test('page and docked viewer use the same envelope partial',()=>{
  for(const file of ['base.html','pages/session-view.html']) {
    assert.match(fs.readFileSync(path.join(__dirname,'../templates',file),'utf8'),/include "partials\/session-approval-button.html"/);
  }
});
