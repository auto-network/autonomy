/* Cross-implementation parity: the browser openContentKey must open a CEK
 * sealed by the Python tools/vault/policy_class.py::seal_cek byte-for-byte.
 *
 * The vector below was emitted by the Python side (a password policy class + a
 * personal secured revision + open_cek); if the JS composition, any purpose
 * string, or the canonical-JSON/SHA-256 digest drifts from Python, this fails.
 * Regenerate with the emitter documented in the B-1 change if the sealing
 * construction ever changes.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { openContentKey } from '../policy-class-open.js';

const VECTOR = {
  bundle: {
    class_id: 'd9b0b28859887030d9824ece818c1f35',
    policy: 'password',
    genesis_id:
      'cb32d511817d3026316ac4a1b2a7fcc68cb3166609d6eefb11932661fee1c00f',
    setting_name:
      '91918db1bcf80e839be913185acb6b2aba649c60b7e6f0461c130fbb19402191',
    generation: {
      gen_id: '8eeb6373db5edfe8f1e61741',
      sealing_public_key:
        'f0147bf5564b784f6ae38e97234c0dcdd5d70ab96ee56f2bda2ae1e6985ea95d',
      wraps: [
        {
          factor_id: 'operator-password',
          role: 'single',
          wrapped:
            '01d6404b47b5c9dcb06b0149b68c599a4e601a8c2908f13e2b844d651edf19bc3d6d897f2555a339363656f9776e9de9f796d05a07ab6e3a9dfe4b907986dea54cb63175da733812a64b2fdfc95c7a8821',
        },
        {
          factor_id: 'personal-root-default',
          role: 'single',
          wrapped:
            '01762451d92b2d9107330add90e59141c3afd9f756a2ce2a83bef8f9b66e25810e8a806eae11b6d2ba17a5a7f374210e196e355a820474b880b4966bb927e89b8fac02740f925b54a198231d1a80448f4e',
        },
      ],
    },
    sealed_cek: {
      ciphertext:
        '01a9137c6d7696fc79c4ed45b1088ebc41eeab545c6b900da5e42921d83042f37189f0729b80573a74a6c67bbd55c17cc5575bcd16e750a16ec2201b8efa34b2f5196372bc7e43c41b3c8dfef4f9d4b644',
      format: 'hpke-x25519-v1',
      gen_id: '8eeb6373db5edfe8f1e61741',
    },
  },
  openers: {
    'operator-password':
      '0d48e640e4923c4bddffd7175590db3dfee53ed595a57979075d9f2e89770d02',
  },
  expected_cek:
    '8fe5a40dbcc58fdec4d4e6aff64b138b43dc646e8324e155b4db5647fc3279cd',
};

test('openContentKey opens a Python-sealed CEK byte-for-byte (parity)', async () => {
  const cek = await openContentKey(VECTOR.bundle, VECTOR.openers);
  assert.equal(cek, VECTOR.expected_cek);
});

test('a wrong opener yields no content key (fails closed)', async () => {
  await assert.rejects(
    openContentKey(VECTOR.bundle, {
      'operator-password': '00'.repeat(32),
    }),
  );
});

test('the two-of-two policy is refused in the browser (out of phase-1 scope)', async () => {
  await assert.rejects(
    openContentKey({ ...VECTOR.bundle, policy: 'both' }, VECTOR.openers),
    /two-of-two/,
  );
});
