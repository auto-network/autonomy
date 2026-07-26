/*
 * Persistence adapters for shared browser and Node ceremonies.
 *
 * Session validation belongs to the ceremony core. This module only
 * round-trips the established record shape while ensuring a Node file never
 * receives the live, non-extractable WebCrypto private key.
 */

/**
 * @typedef {Object} SessionRecord
 * @property {CryptoKey|null} key Non-extractable Ed25519 private signing key.
 *   This is null only when metadata was reloaded from a Node file.
 * @property {string} certWire
 * @property {string} org
 * @property {string} registryUrl
 * @property {string} rootPub
 * @property {string|null} orgSlug
 * @property {number} createdAt
 */

/**
 * @typedef {Object} CeremonyStorage
 * @property {function(): Promise<SessionRecord|null>} getSession
 * @property {function(SessionRecord): Promise<void>} putSession
 * @property {function(): Promise<void>} clearSession
 * @property {function(): Promise<string|null>} getSubjectId
 * @property {function(string): Promise<void>} setSubjectId
 */

const DB_NAME = 'autonomy-network';
const DB_STORE = 'session';
const DB_KEY = 'current';
const SUBJECT_ID_KEY = 'autonomy.network.browser-id';

const SERIALIZABLE_SESSION_FIELDS = [
  'certWire',
  'org',
  'registryUrl',
  'rootPub',
  'orgSlug',
  'createdAt',
];

let nodeFs = null;
if (
  typeof process !== 'undefined'
  && process.versions?.node
) {
  nodeFs = await import('node:fs');
}

function openBrowserDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => {
      request.result.createObjectStore(DB_STORE);
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function browserDatabaseOperation(mode, operation) {
  const database = await openBrowserDatabase();
  try {
    return await new Promise((resolve, reject) => {
      const store = database
        .transaction(DB_STORE, mode)
        .objectStore(DB_STORE);
      const request = operation(store);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  } finally {
    database.close();
  }
}

/** @returns {CeremonyStorage} */
function createBrowserStorage() {
  return {
    async getSession() {
      const record = await browserDatabaseOperation(
        'readonly',
        (store) => store.get(DB_KEY),
      );
      return record === undefined ? null : record;
    },

    async putSession(record) {
      await browserDatabaseOperation(
        'readwrite',
        (store) => store.put(record, DB_KEY),
      );
    },

    async clearSession() {
      await browserDatabaseOperation(
        'readwrite',
        (store) => store.clear(),
      );
    },

    async getSubjectId() {
      try {
        return localStorage.getItem(SUBJECT_ID_KEY);
      } catch {
        return null;
      }
    },

    async setSubjectId(id) {
      try {
        localStorage.setItem(SUBJECT_ID_KEY, id);
      } catch {
        // Browser privacy modes may deny localStorage. Match current behavior.
      }
    },
  };
}

function serializableSession(record) {
  if (record === null) return null;
  const serialized = {};
  for (const field of SERIALIZABLE_SESSION_FIELDS) {
    serialized[field] = (
      field === 'orgSlug' && record[field] === undefined
        ? null
        : record[field]
    );
  }
  return serialized;
}

function liveSessionFromFile(record) {
  if (record === null || record === undefined) return null;
  return {
    key: null,
    ...serializableSession(record),
  };
}

/**
 * Create memory storage with an optional metadata-only JSON mirror.
 *
 * A fresh adapter loading that file returns the serializable session fields
 * with `key: null`; no private key crosses a process boundary. The ceremony
 * caller must therefore treat a reloaded record as unavailable for signing
 * until a live key is installed in this process.
 *
 * @param {{filePath?: string}} options
 * @returns {CeremonyStorage}
 */
function createNodeStorage(options = {}) {
  if (nodeFs === null) {
    throw new Error('createNodeStorage requires the Node runtime');
  }
  const { filePath } = options;
  if (filePath !== undefined && typeof filePath !== 'string') {
    throw new Error('filePath must be a string when provided');
  }

  const memory = {
    session: null,
    subjectId: null,
  };

  if (filePath && nodeFs.existsSync(filePath)) {
    const persisted = JSON.parse(nodeFs.readFileSync(filePath, 'utf8'));
    memory.subjectId = (
      typeof persisted.subjectId === 'string'
        ? persisted.subjectId
        : null
    );
    memory.session = liveSessionFromFile(persisted.session);
  }

  function writeThrough() {
    if (!filePath) return;
    nodeFs.writeFileSync(
      filePath,
      `${JSON.stringify({
        session: serializableSession(memory.session),
        subjectId: memory.subjectId,
      })}\n`,
      'utf8',
    );
  }

  return {
    async getSession() {
      return memory.session;
    },

    async putSession(record) {
      memory.session = record;
      writeThrough();
    },

    async clearSession() {
      memory.session = null;
      writeThrough();
    },

    async getSubjectId() {
      return memory.subjectId;
    },

    async setSubjectId(id) {
      memory.subjectId = id;
      writeThrough();
    },
  };
}

export {
  createBrowserStorage,
  createNodeStorage,
};
