/* Personal secured-setting entry surface.
 *
 * The browser opens the personal root only when it must create the one stable
 * root anchor. Thereafter writes use the class's public sealing key and need
 * no factor gesture. Raw values are accepted only from this field or a local
 * file and are immediately submitted to the trusted personal routing seam;
 * they are never placed in argv, an environment variable, or browser storage.
 */
import { openRoot } from './ceremony/open-root.js';
import { createRootAnchorEnvelope } from './ceremony/root-anchor.js';

const STYLE = `
.vm-overlay{position:fixed;inset:0;z-index:1100;overflow:auto;background:rgba(3,5,10,.88);
  padding:max(28px,env(safe-area-inset-top)) 16px max(28px,env(safe-area-inset-bottom));
  color:#e5e7eb;font:15px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
.vm-card{width:min(100%,560px);margin:4vh auto;background:#11141c;border:1px solid #252c3a;
  border-radius:18px;box-shadow:0 26px 70px rgba(0,0,0,.55);overflow:hidden}
.vm-head{padding:20px 22px;border-bottom:1px solid #252c3a;display:flex;gap:14px;align-items:start}
.vm-title{font-size:18px;font-weight:650}.vm-sub{font-size:12.5px;color:#9aa3b2;margin-top:3px;line-height:1.5}
.vm-close{margin-left:auto;border:0;background:transparent;color:#8b93a3;font-size:24px;cursor:pointer}
.vm-body{padding:22px}.vm-state{border:1px solid #2e3748;background:#171c27;border-radius:12px;
  padding:12px 14px;margin-bottom:18px;font-size:13px;color:#b8c0ce}
.vm-state strong{display:block;color:#f1f5f9;margin-bottom:2px}.vm-label{display:block;color:#cbd5e1;
  font-size:13px;font-weight:600;margin:14px 0 6px}.vm-input,.vm-value{width:100%;box-sizing:border-box;
  border:1px solid #374151;border-radius:10px;background:#1b2230;color:#f1f5f9;padding:12px;
  font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;outline:none}
.vm-value{min-height:210px;resize:vertical}.vm-input:focus,.vm-value:focus{border-color:#818cf8}
.vm-file{display:flex;align-items:center;gap:10px;margin-top:8px;font-size:12px;color:#9aa3b2}
.vm-btn{width:100%;border:0;border-radius:11px;background:#5b57e8;color:white;padding:13px 16px;
  margin-top:18px;font-size:14px;font-weight:650;cursor:pointer}.vm-btn:disabled{opacity:.5;cursor:wait}
.vm-setup{background:#d97706}.vm-note{font-size:11.5px;color:#8b93a3;line-height:1.55;margin-top:10px}
.vm-error{color:#fca5a5;background:#2b1616;border:1px solid #4b2222;border-radius:10px;
  padding:10px 12px;margin-top:12px;font-size:12px}.vm-ok{color:#86efac;background:#11251a;
  border:1px solid #235333;border-radius:10px;padding:12px;margin-top:12px;font-size:13px}
`;

function injectStyles() {
  if (document.getElementById('vault-management-styles')) return;
  const style = document.createElement('style');
  style.id = 'vault-management-styles';
  style.textContent = STYLE;
  document.head.appendChild(style);
}

async function jsonFetch(url, options = {}) {
  const response = await fetch(url, {
    credentials: 'same-origin',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `request failed (${response.status})`);
  return body;
}

function rootClass(inventory) {
  return (inventory.classes || []).find(
    (item) => item.governance && item.governance.form === 'root-reachable',
  ) || null;
}

export async function open() {
  injectStyles();
  const overlay = document.createElement('div');
  overlay.className = 'vm-overlay';
  overlay.setAttribute('data-testid', 'vault-management');
  overlay.innerHTML = `
    <div class="vm-card">
      <div class="vm-head">
        <div><div class="vm-title">Secure a credential</div>
          <div class="vm-sub">Seal a value into your personal store. Reading it later requires an approval and any factor that can open your personal root.</div></div>
        <button class="vm-close" type="button" aria-label="Close">×</button>
      </div>
      <div class="vm-body"><div class="vm-state" data-state></div><div data-form></div></div>
    </div>`;
  document.body.appendChild(overlay);
  const close = () => { if (overlay.parentNode) overlay.remove(); };
  overlay.querySelector('.vm-close').onclick = close;
  overlay.addEventListener('click', (event) => { if (event.target === overlay) close(); });
  const state = overlay.querySelector('[data-state]');
  const form = overlay.querySelector('[data-form]');
  let inventory;

  function setState(title, detail) {
    state.textContent = '';
    const strong = document.createElement('strong'); strong.textContent = title;
    state.appendChild(strong);
    state.appendChild(document.createTextNode(detail));
  }

  function showError(error) {
    const old = form.querySelector('.vm-error'); if (old) old.remove();
    const box = document.createElement('div'); box.className = 'vm-error';
    box.textContent = (error && error.message) || String(error); form.appendChild(box);
  }

  async function load() {
    inventory = await jsonFetch('/api/identity/vault-anchors', { method: 'GET' });
    render();
  }

  async function setup() {
    const opened = await openRoot({
      title: 'Set up personal vault access',
      detail: 'Create one stable vault anchor carried by your personal root.',
    });
    if (!opened) return;
    try {
      const anchor = await createRootAnchorEnvelope(opened, {
        anchorId: 'personal-root-default',
        displayName: 'Personal root vault',
      });
      const enrolled = await jsonFetch('/api/identity/vault-anchors', {
        method: 'POST', body: JSON.stringify({ anchor }),
      });
      await jsonFetch(`/api/identity/vault-anchors/${encodeURIComponent(enrolled.anchor.anchor_id)}/classes`, {
        method: 'POST', body: JSON.stringify({ display_name: 'Personal root vault' }),
      });
      await load();
    } finally {
      opened.seed.fill(0);
      opened.seed = null;
    }
  }

  function render() {
    const policy = rootClass(inventory);
    form.innerHTML = '';
    if (!policy) {
      setState('One-time setup required', 'No personal-root vault anchor exists yet.');
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'vm-btn vm-setup';
      button.textContent = 'Open my root & set up';
      button.onclick = async () => {
        button.disabled = true;
        try { await setup(); } catch (error) { showError(error); }
        finally { button.disabled = false; }
      };
      form.appendChild(button);
      return;
    }
    setState(
      policy.governance.display_name,
      'Root-reachable · writes use the public sealing key · reads require your root gesture',
    );
    form.innerHTML = `
      <label class="vm-label" for="vm-key">Credential name</label>
      <input class="vm-input" id="vm-key" data-key value="mac.ssh.disposable-proof" autocomplete="off" spellcheck="false">
      <label class="vm-label" for="vm-value">Private value</label>
      <textarea class="vm-value" id="vm-value" data-value autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Paste a disposable private key here"></textarea>
      <label class="vm-file">Or load a local file <input type="file" data-file></label>
      <button class="vm-btn" type="button" data-seal>Seal into my personal vault</button>
      <div class="vm-note">The value is sent only to the trusted dashboard process for immediate public-key sealing. The database stores ciphertext; the value is not retained in this form after the write.</div>`;
    const key = form.querySelector('[data-key]');
    const value = form.querySelector('[data-value]');
    const file = form.querySelector('[data-file]');
    const seal = form.querySelector('[data-seal]');
    file.onchange = async () => {
      if (file.files && file.files[0]) value.value = await file.files[0].text();
    };
    seal.onclick = async () => {
      const raw = value.value;
      if (!key.value.trim() || !raw) { showError(new Error('Enter a credential name and value.')); return; }
      seal.disabled = true;
      try {
        const result = await jsonFetch('/api/identity/vault-settings', {
          method: 'POST',
          body: JSON.stringify({
            key: key.value.trim(), value: raw, policy_class_id: policy.class_id,
          }),
        });
        value.value = ''; file.value = '';
        const ok = document.createElement('div'); ok.className = 'vm-ok';
        ok.textContent = `${result.key} was sealed. The stored row contains ciphertext only.`;
        form.appendChild(ok);
      } catch (error) { showError(error); }
      finally { seal.disabled = false; }
    };
  }

  setState('Loading personal vault…', 'Checking the stable root anchor and policy class.');
  try { await load(); } catch (error) { form.innerHTML = ''; showError(error); }
}

export default { open };
