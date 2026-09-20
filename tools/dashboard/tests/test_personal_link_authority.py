"""The real browser certificate verifies without a personal membership ledger."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.dashboard import identity_routes, link_approvals
from tools.network.idkit import KeyPair, derive_machine_key
from tools.network.idkit.root_factor_policy import mint_password_armor


@pytest.fixture(params=[False, True], ids=["session", "first-publication"])
def signed(monkeypatch, request):
    root = KeyPair.generate()
    binding = {"org_uuid": "11111111-1111-4111-8111-111111111111",
               "root_pub": root.public_hex, "registry_url": "https://registry.test"}
    monkeypatch.setattr(identity_routes, "_personal_member", lambda: SimpleNamespace(payload={"root_pub": root.public_hex}))
    monkeypatch.setattr(link_approvals, "_authorization_refusal",
                        lambda *args: pytest.fail("personal publication consulted a membership ledger"))
    machine_id = "ab" * 32
    machine = derive_machine_key(bytes.fromhex(root.private_hex), machine_id)
    fixture = {"binding": binding, "armor": mint_password_armor(root, "test-password", iterations=10000),
               "requireServingRuntime": request.param,
               "runtime": {"enabled": True, "serves": True,
                           "personal_root_pub": root.public_hex, "machine_id": machine_id,
                           "machine_pub": machine.public_hex, "org_uuid": binding["org_uuid"],
                           "serving_orgs": [], "sync_orgs": []}}
    script = r"""
import assert from 'node:assert/strict';
import {configure, signOn, signRegistryRequest} from '../static/js/network-signon.mjs';
const f = JSON.parse(process.argv[1]);
let stored;
let runtimePosts = 0;
configure({storage: {
  getSession: async()=>stored, putSession: async value=>{stored=value}, clearSession:async()=>{stored=null},
  getSubjectId:async()=>null, setSubjectId:async()=>{},
}, transport:{fetch:async (url, options)=>{
  if(url==='/api/identity/personal') return Response.json({armored_private_key:f.armor,root_pub:f.binding.root_pub});
  if(url==='/api/network/binding?org=personal') return Response.json(f.binding);
  if(url==='/api/network/serve-cert?org=personal') return Response.json({status:'ok'});
  if(url==='/api/fleet/runtime') {
    if(options?.method==='POST') {
      const body=JSON.parse(options.body);
      assert.equal(body.machine_pub,f.runtime.machine_pub);
      assert.equal(body.reachability_cert.org,f.binding.org_uuid);
      runtimePosts++;
      return Response.json({ok:true});
    }
    return Response.json(f.runtime);
  }
  throw new Error('Unexpected dependency: '+url);
}}});
await signOn('test-password',{org:'personal',requireServingRuntime:f.requireServingRuntime});
assert.equal(runtimePosts,f.requireServingRuntime?1:0);
assert.equal(stored.key.extractable,false);
const entry=Object.values(stored.orgs)[0];
assert.equal(entry.genesisId,null);
const publish=await signRegistryRequest('TUNNEL','/control/create-link',{org:f.binding.org_uuid},{org:'personal'});
const revoke=await signRegistryRequest('TUNNEL','/control/revoke-link',{}, {org:'personal'});
console.log(JSON.stringify({publish,revoke}));
"""
    result = subprocess.run(["node", "--input-type=module", "-e", script, json.dumps(fixture)],
                            cwd=Path(__file__).parent, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return root, binding, json.loads(result.stdout)


@pytest.mark.parametrize("op,scope,path", [
    ("publish", "link:publish", "/control/create-link"),
    ("revoke", "link:revoke", "/control/revoke-link"),
])
def test_personal_certificate(signed, op, scope, path):
    root, binding, envelopes = signed
    subject = {"kind": "operator", "id": root.public_hex}
    verify = link_approvals._verify_local_publish_authority
    assert verify(envelopes[op], subject, "personal", binding, scope, path) is None
    assert verify(envelopes[op], subject, "personal", binding, "tunnel:serve", path)
    assert verify(envelopes[op], subject, "personal", {**binding, "root_pub": KeyPair.generate().public_hex}, scope, path)
    assert verify({**envelopes[op], "sig": "00" * 64}, subject, "personal", binding, scope, path)
    assert verify({**envelopes[op], "ts": 0}, subject, "personal", binding, scope, path)
