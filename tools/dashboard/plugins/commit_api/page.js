// Commit-signing operator review plugin — frontend Alpine factory.
//
// Read-only master/detail UI for the "commits awaiting signature"
// queue. Consumes two same-origin REST endpoints (no auth header):
//
//   GET /api/capabilities/commit/v1/operator/signing-requests
//        -> { count, queue: [ { signing_request_id, workflow_id, ... } ] }
//   GET /api/capabilities/commit/v1/operator/workflows/{workflow_id}
//        -> { workflow, signing_requests: [ { ..., payload } ] }
//
// This surface does NOT perform any signing/attach/publish action —
// that happens on the operator's own device in a later increment. It
// only lets the operator review exactly what they would be signing.

const COMMIT_API_BASE = '/api/capabilities/commit/v1/operator';

// Given epoch seconds, return a compact "waiting" label:
// "just now" / "Nm" / "Nh" / "Nd". Defensive: bad input -> ''.
function commitRelativeTime(epochSeconds) {
  if (typeof epochSeconds !== 'number' || !isFinite(epochSeconds)) return '';
  const diffMs = Date.now() - epochSeconds * 1000;
  if (diffMs < 0) return 'just now';
  if (diffMs < 60_000) return 'just now';
  const m = Math.floor(diffMs / 60_000);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  const d = Math.floor(h / 24);
  return `${d}d`;
}

function commitApiPage() {
  return {
    // ---- queue state ----
    queueLoading: true,
    queueError: null,        // { status, message } | null
    count: 0,
    queue: [],

    // ---- selection / detail state ----
    selectedId: null,        // selected signing_request_id
    selectedWorkflowId: null,
    detailLoading: false,
    detailError: null,       // { status, message } | null
    detail: null,            // { workflow, signing_requests }

    async init() {
      await this.loadQueue();
    },

    async loadQueue() {
      this.queueLoading = true;
      this.queueError = null;
      try {
        const res = await fetch(`${COMMIT_API_BASE}/signing-requests`, {
          headers: { Accept: 'application/json' },
        });
        if (!res.ok) {
          this.queueError = await this._errorFrom(res);
          this.queue = [];
          this.count = 0;
          return;
        }
        const data = await res.json();
        this.queue = Array.isArray(data && data.queue) ? data.queue : [];
        this.count = typeof (data && data.count) === 'number'
          ? data.count
          : this.queue.length;

        // Keep a valid selection: if the selected row vanished from the
        // queue, clear the detail panel so we don't show a stale commit.
        if (this.selectedId &&
            !this.queue.some((r) => r && r.signing_request_id === this.selectedId)) {
          this.clearSelection();
        }
      } catch (err) {
        this.queueError = { status: null, message: this._msg(err) };
        this.queue = [];
        this.count = 0;
      } finally {
        this.queueLoading = false;
      }
    },

    clearSelection() {
      this.selectedId = null;
      this.selectedWorkflowId = null;
      this.detail = null;
      this.detailError = null;
      this.detailLoading = false;
    },

    async selectRow(row) {
      if (!row || !row.workflow_id) return;
      this.selectedId = row.signing_request_id || null;
      this.selectedWorkflowId = row.workflow_id;
      await this.loadDetail(row.workflow_id);
    },

    async retryDetail() {
      if (this.selectedWorkflowId) {
        await this.loadDetail(this.selectedWorkflowId);
      }
    },

    async loadDetail(workflowId) {
      this.detailLoading = true;
      this.detailError = null;
      this.detail = null;
      try {
        const res = await fetch(
          `${COMMIT_API_BASE}/workflows/${encodeURIComponent(workflowId)}`,
          { headers: { Accept: 'application/json' } },
        );
        if (!res.ok) {
          this.detailError = await this._errorFrom(res);
          return;
        }
        // Guard against the selection changing while this was in flight.
        const data = await res.json();
        if (this.selectedWorkflowId !== workflowId) return;
        this.detail = data && typeof data === 'object' ? data : null;
      } catch (err) {
        this.detailError = { status: null, message: this._msg(err) };
      } finally {
        if (this.selectedWorkflowId === workflowId) {
          this.detailLoading = false;
        }
      }
    },

    // ---- derived helpers (used from the template) ----

    subjectOf(row) {
      const s = row && row.message_subject;
      return (typeof s === 'string' && s.trim()) ? s : '(no subject)';
    },

    waitingLabel(epochSeconds) {
      return commitRelativeTime(epochSeconds);
    },

    isChain(row) {
      return !!(row && typeof row.batch_size === 'number' && row.batch_size > 1);
    },

    chainLabel(row) {
      if (!this.isChain(row)) return '';
      const pos = (row && typeof row.position_in_batch === 'number')
        ? row.position_in_batch : '?';
      return `chain ${pos}/${row.batch_size}`;
    },

    // The primary signing request within the loaded workflow detail.
    // Prefers the one matching the selected row, else the first.
    get activeSigningRequest() {
      const reqs = this.detail && Array.isArray(this.detail.signing_requests)
        ? this.detail.signing_requests : [];
      if (!reqs.length) return null;
      if (this.selectedId) {
        const match = reqs.find(
          (r) => r && r.signing_request_id === this.selectedId,
        );
        if (match) return match;
      }
      return reqs[0];
    },

    get activeWorkflow() {
      return (this.detail && this.detail.workflow) || null;
    },

    get activePreview() {
      const req = this.activeSigningRequest;
      return (req && req.payload && req.payload.canonical_payload_preview) || null;
    },

    identityLabel(person) {
      if (!person || typeof person !== 'object') return '';
      const name = typeof person.name === 'string' ? person.name : '';
      const email = typeof person.email === 'string' ? person.email : '';
      if (name && email) return `${name} <${email}>`;
      return name || email || '';
    },

    parentShas(preview) {
      const p = preview && preview.parent_shas;
      return Array.isArray(p) ? p.filter((x) => typeof x === 'string') : [];
    },

    // ---- internal utilities ----

    async _errorFrom(res) {
      let message = `Request failed (HTTP ${res.status})`;
      try {
        const body = await res.json();
        if (body && typeof body === 'object') {
          if (typeof body.message === 'string' && body.message) {
            message = body.message;
          } else if (typeof body.code === 'string' && body.code) {
            message = body.code;
          }
        }
      } catch (_e) {
        // Non-JSON body; keep the generic message.
      }
      if (res.status === 404) {
        message = message || 'Workflow not found';
      }
      return { status: res.status, message };
    },

    _msg(err) {
      if (err && typeof err.message === 'string' && err.message) return err.message;
      return 'Network request failed';
    },
  };
}

// The plugin loader evaluates this file with ``Alpine`` in scope and
// expects the component to be registered here (matching the stub).
if (typeof Alpine !== 'undefined') {
  Alpine.data('commitApiPage', commitApiPage);
}

// Node-side test harness hook (parity with sibling plugins).
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { commitApiPage, commitRelativeTime };
}
