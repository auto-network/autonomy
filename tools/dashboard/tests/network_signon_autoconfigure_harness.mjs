import { webcrypto } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

globalThis.crypto = webcrypto;

const mode = process.argv[2];
const moduleUrl = pathToFileURL(path.join(
  path.dirname(new URL(import.meta.url).pathname),
  '..',
  'static',
  'js',
  'network-signon.mjs',
)).href;

function installIndexedDb() {
  const records = new Map();
  let initialized = false;

  function request(resultOperation) {
    const result = {};
    queueMicrotask(() => {
      try {
        result.result = resultOperation();
        if (result.onsuccess) result.onsuccess();
      } catch (error) {
        result.error = error;
        if (result.onerror) result.onerror();
      }
    });
    return result;
  }

  const objectStore = {
    get(key) {
      return request(() => records.get(key));
    },
    put(value, key) {
      return request(() => {
        records.set(key, value);
        return key;
      });
    },
    clear() {
      return request(() => {
        records.clear();
        return undefined;
      });
    },
    count() {
      return request(() => records.size);
    },
  };
  const database = {
    createObjectStore() {
      initialized = true;
      return objectStore;
    },
    transaction() {
      return {
        objectStore() {
          return objectStore;
        },
      };
    },
    close() {},
  };
  globalThis.indexedDB = {
    open() {
      const openRequest = {};
      queueMicrotask(() => {
        openRequest.result = database;
        if (!initialized && openRequest.onupgradeneeded) {
          openRequest.onupgradeneeded();
        }
        initialized = true;
        if (openRequest.onsuccess) openRequest.onsuccess();
      });
      return openRequest;
    },
  };
  return records;
}

async function browserMode() {
  const records = installIndexedDb();
  const localValues = new Map();
  const fetchCalls = [];
  globalThis.localStorage = {
    getItem(key) {
      return localValues.has(key) ? localValues.get(key) : null;
    },
    setItem(key, value) {
      localValues.set(key, String(value));
    },
  };
  const binding = {
    org_uuid: process.env.AUTONOMY_ORG_UUID,
    root_pub: process.env.AUTONOMY_ROOT_PUB,
    registry_url: 'https://registry.module-load.test',
  };
  globalThis.window = {
    async fetch(url) {
      fetchCalls.push(String(url));
      if (String(url).startsWith('/api/network/org-key')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            armored_private_key: process.env.AUTONOMY_ARMOR,
            root_pub: process.env.AUTONOMY_ROOT_PUB,
          }),
        };
      }
      if (String(url).startsWith('/api/network/binding')) {
        return {
          ok: true,
          status: 200,
          json: async () => binding,
        };
      }
      return {
        ok: false,
        status: 404,
        json: async () => ({ error: 'not found' }),
      };
    },
  };

  // This import is the action under test. No configure call follows it.
  await import(moduleUrl);
  const session = window.AutonomyNetworkSession;
  await session.ready();
  const signOnResult = await session.signOn(
    process.env.AUTONOMY_PASSPHRASE,
    { org: 'module-load-org', ttlSeconds: 3600 },
  );
  const envelope = await window.AutonomyNetworkSigner.signRegistryRequest(
    'POST',
    '/v1/links',
    {
      org: binding.org_uuid,
      target_uuid: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
      target_type: 'present',
    },
  );
  const output = {
    signOnResult,
    envelope,
    state: session.state(),
    storedSessions: records.size,
    subjectId: localValues.get('autonomy.network.browser-id') || null,
    fetchCalls,
  };
  process.stdout.write(JSON.stringify(output), () => process.exit(0));
}

async function nodeMode() {
  delete globalThis.window;
  delete globalThis.indexedDB;
  delete globalThis.localStorage;
  const module = await import(moduleUrl);
  let rejection = null;
  try {
    await module.signRegistryRequest('POST', '/v1/links', {});
  } catch (error) {
    rejection = error.message || String(error);
  }
  process.stdout.write(JSON.stringify({
    exports: Object.keys(module).sort(),
    windowType: typeof globalThis.window,
    rejection,
  }));
}

if (mode === 'browser') {
  browserMode().catch((error) => {
    process.stderr.write(`${error.stack || error}\n`);
    process.exit(1);
  });
} else if (mode === 'node') {
  nodeMode().catch((error) => {
    process.stderr.write(`${error.stack || error}\n`);
    process.exit(1);
  });
} else {
  process.stderr.write('expected browser or node mode\n');
  process.exit(2);
}
